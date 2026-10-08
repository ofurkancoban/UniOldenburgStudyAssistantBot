import dotenvModule from 'dotenv';
dotenvModule.config({ path: '../.env' });

import express from 'express';
import makeWASocket, { useMultiFileAuthState, DisconnectReason, jidNormalizedUser } from '@whiskeysockets/baileys';
import { pino } from 'pino';
import qrcodeTerminal from 'qrcode-terminal';
import qrcode from 'qrcode';
import FormData from 'form-data';
import https from 'https';
import fs from 'fs';

// The libsignal dependency (used internally by Baileys for the Signal
// protocol encryption) calls console.info()/console.warn() directly with
// raw session objects - including private key material (privKey, rootKey,
// baseKey) - completely bypassing the pino logger passed into
// makeWASocket. That means it would otherwise land in plaintext in pm2's
// log files. We never call console.info/console.warn ourselves (only
// console.log/console.error throughout this file), so silencing them here
// only mutes that noisy, sensitive third-party logging - nothing of ours.
console.info = () => {};
console.warn = () => {};

const app = express();
app.use(express.json());

const PORT = process.env.PORT || 3838;
const TELEGRAM_TOKEN = process.env.TELEGRAM_TOKEN;
const ALLOWED_USER_IDS = process.env.ALLOWED_USER_IDS ? process.env.ALLOWED_USER_IDS.split(',') : [];
const AUTH_DIR = 'baileys_auth';

// Safety net: an error from an internal async path not awaited by our own
// try/catch is an unhandled rejection/exception and would otherwise exit
// Node, killing the whole service (and pm2 then restarts it from scratch,
// dropping the session). Log and keep running instead; reconnectClient()'s
// own guard (isReconnecting) prevents pile-ups if this fires mid-reconnect.
process.on('unhandledRejection', (err) => {
    console.error('Unhandled rejection (kept process alive):', err);
});
process.on('uncaughtException', (err) => {
    console.error('Uncaught exception (kept process alive):', err);
});

async function sendQRToTelegram(qrString) {
    if (!TELEGRAM_TOKEN || ALLOWED_USER_IDS.length === 0) return;

    try {
        const qrBuffer = await qrcode.toBuffer(qrString);
        const adminId = ALLOWED_USER_IDS[0].trim();

        const form = new FormData();
        form.append('chat_id', adminId);
        form.append('photo', qrBuffer, { filename: 'qr.png', contentType: 'image/png' });
        form.append('caption', '📲 WhatsApp Web QR Code.\nPlease scan this code with your WhatsApp app to log in.');

        const req = https.request({
            host: 'api.telegram.org',
            path: `/bot${TELEGRAM_TOKEN}/sendPhoto`,
            method: 'POST',
            headers: form.getHeaders()
        }, (res) => {
            if (res.statusCode === 200) {
                console.log('QR Code sent to Telegram admin.');
            } else {
                console.error('Failed to send QR to Telegram:', res.statusCode);
            }
        });

        form.pipe(req);
    } catch (err) {
        console.error('Error sending QR to Telegram:', err);
    }
}

async function sendTextToTelegram(text, replyMarkup) {
    if (!TELEGRAM_TOKEN || ALLOWED_USER_IDS.length === 0) return;

    try {
        const adminId = ALLOWED_USER_IDS[0].trim();
        const body = { chat_id: adminId, text };
        if (replyMarkup) body.reply_markup = replyMarkup;
        const payload = JSON.stringify(body);

        const req = https.request({
            host: 'api.telegram.org',
            path: `/bot${TELEGRAM_TOKEN}/sendMessage`,
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload) }
        }, (res) => {
            res.on('data', () => {});
        });
        req.on('error', (err) => console.error('Error sending text to Telegram:', err));
        req.write(payload);
        req.end();
    } catch (err) {
        console.error('Error sending text to Telegram:', err);
    }
}

let sock = null;
let isAuthenticated = false;
let isReconnecting = false;
let hasEverConnected = false; // true once the connection has opened at least once this process - never reset, so /status can tell "waiting for the first-ever QR" apart from "was connected, session dropped"
const announcedGroupIds = new Set();
const discoveredGroups = new Map(); // groupId -> { name, lastSeen }

// Tracks whether a "🔴 disconnected" alert is currently outstanding, so the
// matching "🟢 reconnected" only fires after a real outage (not on the very
// first startup) and repeated close events while already down don't spam a
// fresh alert each time.
let disconnectAlertPending = false;

// (Re)creates the socket and attaches all event listeners. Baileys has no
// destroy+reinitialize pair like whatsapp-web.js did - "reconnecting" here
// just means discarding the old socket reference and building a fresh one,
// reusing the same useMultiFileAuthState folder so a still-valid session
// restores silently with no new QR.
async function connectToWhatsApp() {
    const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);

    sock = makeWASocket({
        auth: state,
        logger: pino({ level: 'silent' }),
    });

    sock.ev.on('creds.update', saveCreds);

    sock.ev.on('connection.update', (update) => {
        const { connection, lastDisconnect, qr } = update;

        if (qr) {
            console.log('QR Code received, scan please:');
            qrcodeTerminal.generate(qr, { small: true });
            sendQRToTelegram(qr);
        }

        if (connection === 'open') {
            console.log('WhatsApp Client is ready!');
            isAuthenticated = true;
            isReconnecting = false;
            hasEverConnected = true;
            if (disconnectAlertPending) {
                disconnectAlertPending = false;
                sendTextToTelegram('🟢 WhatsApp reconnected — forwarding is back up.');
            }
        }

        if (connection === 'close') {
            isAuthenticated = false;
            const statusCode = lastDisconnect?.error?.output?.statusCode;
            const reasonText = lastDisconnect?.error?.message || String(lastDisconnect?.error || 'unknown');

            if (statusCode === DisconnectReason.loggedOut) {
                // A real logout (force_new_qr, or the account was unlinked from
                // the phone) - the saved session is dead, auto-reconnecting
                // with it would just loop. Wait for an explicit /request_qr or
                // /force_new_qr instead of retrying on our own.
                console.log('WhatsApp session logged out - not auto-reconnecting. Use /request_qr or /force_new_qr to relink.');
                return;
            }

            console.log('WhatsApp client disconnected:', reasonText);
            // Proactive alert instead of only showing up passively in /status -
            // an outage could otherwise go unnoticed for hours. Only the first
            // close event of an outage alerts (see disconnectAlertPending);
            // reconnectClient's own isReconnecting guard already prevents
            // duplicate reconnect attempts if 'close' fires more than once
            // while already down.
            if (!disconnectAlertPending) {
                disconnectAlertPending = true;
                sendTextToTelegram(`🔴 WhatsApp disconnected (${reasonText}) — attempting to reconnect automatically. Forwarding won't work until this recovers.`);
            }
            reconnectClient(`disconnected: ${reasonText}`);
        }
    });

    // Log every message (incoming AND outgoing - messages.upsert fires both
    // ways) so the user can easily discover Group IDs.
    sock.ev.on('messages.upsert', async ({ messages }) => {
        for (const msg of messages) {
            try {
                const groupId = msg.key && msg.key.remoteJid;
                if (!groupId || !groupId.endsWith('@g.us')) continue;

                console.log(`[GROUP ID DISCOVERY] Detected a message in a group! The Group ID is: ${groupId}`);

                // Always keep track of seen groups so they can be listed/set on
                // demand later, even after WA_GROUP_ID is already configured.
                let name = discoveredGroups.get(groupId)?.name;
                try {
                    const metadata = await sock.groupMetadata(groupId);
                    if (metadata && metadata.subject) name = metadata.subject;
                } catch (e) {
                    // best-effort only
                }
                discoveredGroups.set(groupId, { name, lastSeen: Date.now() });

                // Auto-notify admin via Telegram so WA_GROUP_ID can be set
                // without relying on groupFetchAllParticipating()
                if (!process.env.WA_GROUP_ID && !announcedGroupIds.has(groupId)) {
                    announcedGroupIds.add(groupId);
                    sendTextToTelegram(
                        `📍 WhatsApp Group ID detected:\n${groupId}\n\nTap below to set it as the active group.`,
                        { inline_keyboard: [[{ text: '✅ Set as active group', callback_data: `set_wa_group|${groupId}` }]] }
                    );
                }
            } catch (e) {
                console.error('Error in message listener:', e);
            }
        }
    });
}

// Guarded against overlapping calls so a close event, a failed send, and a
// manual /request_qr can't race each other.
//
// If forceLogout is true, the saved session is invalidated first (via
// sock.logout() and clearing the auth folder) so the next connect is forced
// to emit a real QR instead of silently restoring the existing session -
// use this only when the caller explicitly wants a fresh QR (e.g. to link a
// different phone), since it breaks the current connection and requires
// re-scanning.
async function reconnectClient(reason, { forceLogout = false } = {}) {
    if (isReconnecting) {
        console.log(`Reconnect already in progress, skipping duplicate trigger (${reason}).`);
        return;
    }
    isReconnecting = true;
    isAuthenticated = false;
    console.log(`Re-initializing WhatsApp client (${reason})...`);

    if (forceLogout) {
        try {
            if (sock) await sock.logout();
        } catch (err) {
            console.error('Error logging out client:', err);
        }
        try {
            fs.rmSync(AUTH_DIR, { recursive: true, force: true });
        } catch (err) {
            console.error('Error clearing auth state:', err);
        }
    } else {
        try {
            if (sock) sock.end(undefined);
        } catch (err) {
            // ignore - socket may already be closed
        }
    }

    try {
        await connectToWhatsApp();
    } catch (err) {
        console.error('Error re-initializing client:', err);
        isReconnecting = false;
        sendTextToTelegram(`🔴 WhatsApp reconnect attempt failed (${reason}): ${err && err.message ? err.message : String(err)}\n\nWon't retry automatically until another trigger (a send attempt, or tap "📲 Request WA QR" in /status).`);
    }
}

app.post('/request_qr', async (req, res) => {
    // Always force a fresh connection attempt on explicit request, even if
    // isAuthenticated is (possibly stale) true - the operator only calls
    // this when the connection is actually broken, so trust the request
    // over a stale in-memory flag. NOTE: if a valid session is still saved
    // on disk, this just silently restores it and no QR is sent - use
    // /force_new_qr to guarantee a real QR even when currently connected.
    res.json({ status: 'waiting', message: 'Reconnecting. If a saved session is still valid it will restore silently; otherwise a QR code will be sent shortly.' });
    await reconnectClient('manual /request_qr');
});

// Forces a brand new QR code even if currently authenticated, by invalidating
// the saved session first. Use this to link a different phone/account or when
// the connection is stuck in a way a plain reconnect can't fix.
app.post('/force_new_qr', async (req, res) => {
    res.json({ status: 'waiting', message: 'Logging out and generating a brand new QR code. It will be sent to you shortly.' });
    await reconnectClient('manual /force_new_qr', { forceLogout: true });
});

// API endpoint to check authentication status. isAuthenticated alone can't
// tell "never set up yet, waiting for the first QR" apart from "was
// connected, the session just dropped" - isReconnecting/hasEverConnected
// let a caller (see /status in studip_bot.py) show that distinction.
app.get('/status', (req, res) => {
    res.json({ isAuthenticated, isReconnecting, hasEverConnected });
});

// API endpoint to set WA_GROUP_ID at runtime (persisted to .env by the Telegram bot)
app.post('/set_group_id', (req, res) => {
    const { groupId } = req.body;
    if (!groupId) {
        return res.status(400).json({ error: 'Missing groupId in request body' });
    }
    process.env.WA_GROUP_ID = groupId;
    console.log(`WA_GROUP_ID set at runtime to: ${groupId}`);
    res.json({ success: true });
});

// API endpoint to list groups seen so far, regardless of whether WA_GROUP_ID is already set
app.get('/discovered_groups', (req, res) => {
    const groups = Array.from(discoveredGroups.entries())
        .map(([groupId, info]) => ({ groupId, name: info.name, lastSeen: info.lastSeen }))
        .sort((a, b) => b.lastSeen - a.lastSeen);
    res.json({ groups });
});

// Helper to find a group by its display name (kept for API parity with the
// old server - in practice studip_bot.py always sends a raw ID via
// WA_GROUP_ID, never a name, so this path is effectively unused today).
async function findGroupByName(name) {
    const groups = await sock.groupFetchAllParticipating();
    const match = Object.values(groups).find((g) => g.subject === name);
    return match ? { id: { _serialized: match.id } } : null;
}

// API endpoint to resolve a group's current display name directly from
// WhatsApp by its ID - unlike /discovered_groups (which only knows names
// seen via the messages.upsert log and forgets them on every restart), this
// asks WhatsApp directly, so it works even for a group the service has
// never logged a message from in its current run.
app.get('/group_name', async (req, res) => {
    if (!isAuthenticated) {
        return res.status(503).json({ error: 'WhatsApp client is not ready yet.' });
    }
    const groupId = req.query.groupId;
    if (!groupId) {
        return res.status(400).json({ error: 'Missing groupId query param' });
    }
    try {
        const metadata = await sock.groupMetadata(groupId);
        const name = metadata && metadata.subject ? metadata.subject : null;
        if (!name) {
            return res.status(404).json({ error: 'Group not found' });
        }
        res.json({ name });
    } catch (err) {
        console.error('Error resolving group name:', err);
        res.status(500).json({ error: `Internal server error: ${err && err.message ? err.message : String(err)}` });
    }
});

// sock.sendMessage() can resolve to undefined without throwing in some edge
// cases - there's no reliable way to tell from here whether the message
// actually went out and only the confirmation failed, or it never sent at
// all. Given that ambiguity, this deliberately does NOT auto-retry the send
// itself (a message that did go out would then be sent a second time,
// duplicating it in a shared WhatsApp group - a much worse outcome than one
// bounced request). It only kicks off a background reconnect so the *next*
// attempt has better odds; this request still reports failure and leaves
// resending to the caller tapping again.
function noteSendFailureAndReconnect(chatId) {
    console.log(`sendMessage returned no message for ${chatId} - triggering a background reconnect for next time`);
    reconnectClient(`sendMessage returned no message for ${chatId}`);
}

// API endpoint to send a message
app.post('/send', async (req, res) => {
    if (!isAuthenticated) {
        return res.status(503).json({ error: 'WhatsApp client is not ready yet or needs a QR scan. Please wait 30 seconds or request a new QR code.' });
    }

    const { text, groupName } = req.body;

    if (!text || !groupName) {
        return res.status(400).json({ error: 'Missing text or groupName in request body' });
    }

    try {

        // Use hardcoded WA_GROUP_ID from .env if available to completely bypass group lookup
        const hardcodedGroupId = process.env.WA_GROUP_ID;

        if (hardcodedGroupId) {
            // jidNormalizedUser is a no-op for group JIDs (@g.us has no
            // per-device suffix to strip) - this is purely defensive, in
            // case WA_GROUP_ID ever ends up holding a device-specific JID.
            const sent = await sock.sendMessage(jidNormalizedUser(hardcodedGroupId), { text });
            if (!sent) {
                noteSendFailureAndReconnect(hardcodedGroupId);
                return res.status(502).json({ error: 'WhatsApp accepted the request but did not confirm the message was sent (it may or may not have actually gone out - check the group before resending). Reconnecting in the background; try again in about 30 seconds.' });
            }
            console.log(`Message sent directly to WA_GROUP_ID: ${hardcodedGroupId}`);
            return res.json({ success: true, message: 'Message sent via Hardcoded ID' });
        }

        // If the python bot sent an ID instead of a name (ends with @g.us or @c.us), use it directly
        if (groupName.endsWith('@g.us') || groupName.endsWith('@c.us')) {
            const sent = await sock.sendMessage(jidNormalizedUser(groupName), { text });
            if (!sent) {
                noteSendFailureAndReconnect(groupName);
                return res.status(502).json({ error: 'WhatsApp accepted the request but did not confirm the message was sent (it may or may not have actually gone out - check the group before resending). Reconnecting in the background; try again in about 30 seconds.' });
            }
            console.log(`Message sent directly to ID: ${groupName}`);
            return res.json({ success: true, message: 'Message sent via direct ID' });
        }

        // Otherwise try searching by name
        const group = await findGroupByName(groupName);
        if (group) {
            const sent = await sock.sendMessage(group.id._serialized, { text });
            if (!sent) {
                noteSendFailureAndReconnect(group.id._serialized);
                return res.status(502).json({ error: 'WhatsApp accepted the request but did not confirm the message was sent (it may or may not have actually gone out - check the group before resending). Reconnecting in the background; try again in about 30 seconds.' });
            }
            console.log(`Message sent to group "${groupName}"`);
            res.json({ success: true, message: 'Message sent' });
        } else {
            console.log(`Group "${groupName}" not found`);
            res.status(404).json({ error: 'Group not found' });
        }
    } catch (err) {
        console.error('Error sending message:', err);
        const errorString = err && err.message ? err.message : String(err);
        // A thrown exception here (vs. the quieter undefined-return case
        // above) almost always means the socket itself is in a bad state -
        // worth a background reconnect so the next attempt has better odds.
        console.log('Triggering a background reconnect after a send exception...');
        reconnectClient(`send exception: ${errorString}`);
        res.status(500).json({ error: `Internal server error: ${errorString}` });
    }
});

app.listen(PORT, () => {
    console.log(`WhatsApp service listening on port ${PORT}`);
});

// Graceful shutdown on exit (PM2 restart/stop)
const cleanupAndExit = async () => {
    console.log('\nShutting down WhatsApp service...');
    try {
        if (sock) sock.end(undefined);
    } catch (err) {
        // Ignore errors during shutdown
    }
    process.exit(0);
};

process.on('SIGINT', cleanupAndExit);
process.on('SIGTERM', cleanupAndExit);
process.on('SIGQUIT', cleanupAndExit);

connectToWhatsApp();
