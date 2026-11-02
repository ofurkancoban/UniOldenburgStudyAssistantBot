// Drop-in replacement for Baileys' useMultiFileAuthState that survives a
// full disk or a crash mid-write. Same folder layout and file names, so an
// existing baileys_auth folder keeps working unchanged.
//
// Baileys' own version writes each file in place with writeFile(), which
// truncates first: when the disk is full the file is left at 0 bytes, the
// next start finds no creds, silently creates a brand new identity and the
// linked session is lost for good. Here:
//   - every write goes to a temp file, is fsynced, then renamed over the
//     target, so a failed write leaves the previous file untouched;
//   - each successful creds.json save is mirrored to creds.json.bak, and
//     loading falls back to it when creds.json is unreadable;
//   - if both are unreadable while the folder still holds session keys, the
//     folder is moved aside instead of being overwritten by a new identity,
//     so it can still be recovered by hand.
import { Mutex } from 'async-mutex';
import { mkdir, open, readFile, readdir, rename, stat, unlink } from 'fs/promises';
import { join } from 'path';
import { proto, initAuthCreds, BufferJSON } from '@whiskeysockets/baileys';

const CREDS_FILE = 'creds.json';
const CREDS_BACKUP_FILE = 'creds.json.bak';

const fileLocks = new Map();
const getFileLock = (path) => {
    let mutex = fileLocks.get(path);
    if (!mutex) {
        mutex = new Mutex();
        fileLocks.set(path, mutex);
    }
    return mutex;
};

const fixFileName = (file) => file?.replace(/\//g, '__')?.replace(/:/g, '-');

async function atomicWrite(filePath, text) {
    const tmpPath = `${filePath}.tmp-${process.pid}`;
    let handle;
    try {
        handle = await open(tmpPath, 'w');
        await handle.writeFile(text);
        await handle.sync();
        await handle.close();
        handle = null;
        await rename(tmpPath, filePath);
    } catch (err) {
        if (handle) await handle.close().catch(() => {});
        await unlink(tmpPath).catch(() => {});
        throw err;
    }
}

async function readJson(filePath) {
    try {
        const text = await readFile(filePath, { encoding: 'utf-8' });
        return text.trim() ? JSON.parse(text, BufferJSON.reviver) : null;
    } catch {
        return null;
    }
}

// onAlert(text) is called for events the operator should know about (a
// failed save, a restore from backup, a quarantined folder).
export async function useResilientAuthState(folder, { onAlert = () => {} } = {}) {
    const writeData = async (data, file) => {
        const filePath = join(folder, fixFileName(file));
        const release = await getFileLock(filePath).acquire();
        try {
            await atomicWrite(filePath, JSON.stringify(data, BufferJSON.replacer));
        } finally {
            release();
        }
    };
    const readData = async (file) => {
        const filePath = join(folder, fixFileName(file));
        const release = await getFileLock(filePath).acquire();
        try {
            return await readJson(filePath);
        } finally {
            release();
        }
    };
    const removeData = async (file) => {
        const filePath = join(folder, fixFileName(file));
        const release = await getFileLock(filePath).acquire();
        try {
            await unlink(filePath);
        } catch {
            // already gone
        } finally {
            release();
        }
    };

    const folderInfo = await stat(folder).catch(() => null);
    if (folderInfo && !folderInfo.isDirectory()) {
        throw new Error(`found something that is not a directory at ${folder}, either delete it or specify a different location`);
    }
    await mkdir(folder, { recursive: true });

    let creds = await readData(CREDS_FILE);
    if (!creds) {
        creds = await readData(CREDS_BACKUP_FILE);
        if (creds) {
            console.error('creds.json was unreadable, restored the session from creds.json.bak');
            onAlert('⚠️ WhatsApp session file was damaged and has been restored from its backup.');
            await writeData(creds, CREDS_FILE).catch((err) => console.error('Could not rewrite creds.json from backup:', err));
        }
    }
    if (!creds) {
        const leftovers = (await readdir(folder)).filter((f) => f.endsWith('.json'));
        if (leftovers.length) {
            const aside = `${folder}.broken-${Date.now()}`;
            await rename(folder, aside);
            await mkdir(folder, { recursive: true });
            console.error(`No readable creds but ${leftovers.length} session files found, moved them to ${aside}`);
            onAlert(`🔴 WhatsApp session credentials are unreadable and no backup could be used. The old session files were moved to ${aside} on the server; a new QR scan is needed.`);
        }
        creds = initAuthCreds();
    }

    let lastSaveFailed = false;
    return {
        state: {
            creds,
            keys: {
                get: async (type, ids) => {
                    const data = {};
                    await Promise.all(ids.map(async (id) => {
                        let value = await readData(`${type}-${id}.json`);
                        if (type === 'app-state-sync-key' && value) {
                            value = proto.Message.AppStateSyncKeyData.fromObject(value);
                        }
                        data[id] = value;
                    }));
                    return data;
                },
                set: async (data) => {
                    const tasks = [];
                    for (const category in data) {
                        for (const id in data[category]) {
                            const value = data[category][id];
                            const file = `${category}-${id}.json`;
                            tasks.push(value ? writeData(value, file) : removeData(file));
                        }
                    }
                    await Promise.all(tasks);
                },
            },
        },
        saveCreds: async () => {
            try {
                await writeData(creds, CREDS_FILE);
                await writeData(creds, CREDS_BACKUP_FILE);
                if (lastSaveFailed) {
                    lastSaveFailed = false;
                    onAlert('🟢 WhatsApp session is being saved again.');
                }
            } catch (err) {
                console.error('Saving WhatsApp creds failed, the previous file was kept:', err);
                if (!lastSaveFailed) {
                    lastSaveFailed = true;
                    onAlert(`⚠️ Could not save the WhatsApp session (${err.code || err.message}). The last good copy is kept; check free disk space on the server.`);
                }
            }
        },
    };
}
