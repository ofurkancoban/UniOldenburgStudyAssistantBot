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
import { chmod, mkdir, open, readFile, readdir, rename, stat, unlink } from 'fs/promises';
import { join } from 'path';
import { proto, initAuthCreds, BufferJSON } from '@whiskeysockets/baileys';

const CREDS_FILE = 'creds.json';
const CREDS_BACKUP_FILE = 'creds.json.bak';

const fileLocks = new Map();
const credsSaveLock = new Mutex();
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
        handle = await open(tmpPath, 'w', 0o600);
        // A leftover temp file from an older version may have broader
        // permissions; mode on open() does not change an existing file.
        await handle.chmod(0o600);
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
    let text;
    try {
        text = await readFile(filePath, { encoding: 'utf-8' });
    } catch (err) {
        if (err.code === 'ENOENT') return null;
        throw err;
    }
    if (!text.trim()) return null;
    try {
        return JSON.parse(text, BufferJSON.reviver);
    } catch (err) {
        if (err instanceof SyntaxError) return null;
        throw err;
    }
}

async function removeFileIfPresent(filePath) {
    try {
        await unlink(filePath);
    } catch (err) {
        if (err.code !== 'ENOENT') throw err;
    }
}

// onAlert(text) is called for events the operator should know about (a
// failed save, a restore from backup, a quarantined folder).
export async function useResilientAuthState(folder, { onAlert = () => {} } = {}) {
    const pendingWrites = new Set();
    const trackWrite = (operation) => {
        const pending = operation();
        pendingWrites.add(pending);
        void pending.then(
            () => pendingWrites.delete(pending),
            () => pendingWrites.delete(pending),
        );
        return pending;
    };
    const writeText = (text, file) => trackWrite(async () => {
            const filePath = join(folder, fixFileName(file));
            const release = await getFileLock(filePath).acquire();
            try {
                await atomicWrite(filePath, text);
            } finally {
                release();
            }
        });
    const writeData = async (data, file) => writeText(JSON.stringify(data, BufferJSON.replacer), file);
    const readData = async (file) => {
        const filePath = join(folder, fixFileName(file));
        const release = await getFileLock(filePath).acquire();
        try {
            return await readJson(filePath);
        } finally {
            release();
        }
    };
    const removeData = (file) => trackWrite(async () => {
        const filePath = join(folder, fixFileName(file));
        const release = await getFileLock(filePath).acquire();
        try {
            await removeFileIfPresent(filePath);
        } finally {
            release();
        }
    });
    const flushWrites = async () => {
        while (pendingWrites.size) {
            await Promise.allSettled([...pendingWrites]);
        }
    };

    const folderInfo = await stat(folder).catch(() => null);
    if (folderInfo && !folderInfo.isDirectory()) {
        throw new Error(`found something that is not a directory at ${folder}, either delete it or specify a different location`);
    }
    await mkdir(folder, { recursive: true, mode: 0o700 });
    // Existing Baileys installs may have been created with the process umask
    // and expose their private keys to other local users. Tighten the folder
    // and every existing regular file before reading any credentials.
    await chmod(folder, 0o700);
    for (const entry of await readdir(folder, { withFileTypes: true })) {
        if (entry.isFile()) {
            await chmod(join(folder, entry.name), 0o600);
        }
    }

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
            await mkdir(folder, { recursive: true, mode: 0o700 });
            await chmod(folder, 0o700);
            console.error(`No readable creds but ${leftovers.length} session files found, moved them to ${aside}`);
            onAlert(`🔴 WhatsApp session credentials are unreadable and no backup could be used. The old session files were moved to ${aside} on the server; a new QR scan is needed.`);
        }
        creds = initAuthCreds();
    }

    let lastSaveFailed = false;
    const saveCreds = () => trackWrite(async () => {
        // Track the whole operation before waiting for the mutex. Otherwise
        // flushWrites() could observe an empty queue while another creds
        // update is still waiting here, then return before that save starts.
        const release = await credsSaveLock.acquire();
        try {
            // Keep the main file and its recovery copy at the same
            // credential revision. Concurrent creds.update events must
            // not interleave primary/backup writes or preserve a stale
            // backup over a newer primary.
            const snapshot = JSON.stringify(creds, BufferJSON.replacer);
            await writeText(snapshot, CREDS_FILE);
            await writeText(snapshot, CREDS_BACKUP_FILE);
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
        } finally {
            release();
        }
    });

    return {
        flushWrites,
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
        saveCreds,
    };
}
