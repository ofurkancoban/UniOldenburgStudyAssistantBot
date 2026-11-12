import dotenvModule from 'dotenv';
dotenvModule.config({ path: '../.env' });

import express from 'express';
import makeWASocket, { DisconnectReason, jidNormalizedUser } from '@whiskeysockets/baileys';
import { pino } from 'pino';
import qrcode from 'qrcode';
import FormData from 'form-data';
import https from 'https';
import fs from 'fs';
import crypto from 'crypto';
import { useResilientAuthState } from './auth_state.js';

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

const PORT = process.env.PORT || 3838;
// Loopback only by default: the bot is the sole client, and nothing else on
// the network should be able to send messages from this WhatsApp account.
const HOST = process.env.WA_HOST || '127.0.0.1';
// Shared secret with the bot (it generates one per run and passes it in the
// environment when it spawns this service, or WA_SERVICE_TOKEN from .env).
const SERVICE_TOKEN = (process.env.WA_SERVICE_TOKEN || '').trim();

function tokenMatches(received) {
    const a = Buffer.from(String(received || ''));
    const b = Buffer.from(SERVICE_TOKEN);
    // timingSafeEqual compares bytes and throws when UTF-8 byte lengths
    // differ. Compare the buffers, not JS string lengths, so malformed
    // non-ASCII headers are rejected as unauthorized instead of becoming 500s.
    return a.length === b.length && a.length > 0 && crypto.timingSafeEqual(a, b);
}

if (!SERVICE_TOKEN) {
    console.error('WA_SERVICE_TOKEN is not set - every request will be rejected. Start the service from the bot or set it in .env.');
}

// Auth first, body parsing second: an unauthenticated request is rejected
// before any of its body is read or parsed.
app.use((req, res, next) => {
    if (!SERVICE_TOKEN || !tokenMatches(req.get('X-WA-Service-Token'))) {
        return res.status(401).json({ error: 'Unauthorized' });
    }
    next();
});
app.use(express.json({ limit: '1mb' }));
const TELEGRAM_TOKEN = process.env.TELEGRAM_TOKEN;
// The QR code and alerts go to the bot's primary administrator (WA_NOTIFY_USER_ID, set by the
// bot), else to the first ALLOWED_USER_IDS entry.
const ALLOWED_USER_IDS = process.env.WA_NOTIFY_USER_ID
    ? [process.env.WA_NOTIFY_USER_ID]
    : (process.env.ALLOWED_USER_IDS ? process.env.ALLOWED_USER_IDS.split(',') : []);
const AUTH_DIR = 'baileys_auth';
const LOCK_FILE = 'whatsapp_service.lock';

function acquireProcessLock() {
    let fd;
    try {
        // Exclusive creation makes the check and claim one atomic operation.
        fd = fs.openSync(LOCK_FILE, 'wx', 0o600);
        fs.writeFileSync(fd, String(process.pid));
        fs.closeSync(fd);
        return true;
    } catch (err) {
        if (fd !== undefined) {
            try { fs.closeSync(fd); } catch { /* ignore */ }
            try { fs.unlinkSync(LOCK_FILE); } catch { /* ignore */ }
        }
        if (err.code !== 'EEXIST') {
            console.error('FATAL: Unable to acquire WhatsApp process lock:', err);
            process.exit(1);
            return false;
        }

        let oldPid;
        try {
            const content = fs.readFileSync(LOCK_FILE, 'utf8').trim();
            if (!/^\d+$/.test(content)) throw new Error('Lock file has no valid PID');
            oldPid = Number(content);
            if (!Number.isSafeInteger(oldPid) || oldPid < 1) throw new Error('Lock file has invalid PID');
        } catch (checkError) {
            console.error('FATAL: WhatsApp process lock is unreadable or malformed; refusing to start:', checkError);
            process.exit(1);
            return false;
        }
        try {
            process.kill(oldPid, 0);
            console.error(`FATAL: Another WhatsApp service is already running (PID ${oldPid}). Exiting immediately.`);
        } catch (checkError) {
            if (checkError.code === 'EPERM') {
                console.error(`FATAL: WhatsApp lock owner PID ${oldPid} exists but cannot be inspected. Exiting.`);
            } else if (checkError.code === 'ESRCH') {
                console.error(`FATAL: Stale WhatsApp lock from PID ${oldPid}; the bot supervisor must clean it before restart.`);
            } else {
                console.error('FATAL: Unable to inspect WhatsApp process lock:', checkError);
            }
        }
        process.exit(1);
        return false;
    }
}

function releaseProcessLock() {
    try {
        if (fs.existsSync(LOCK_FILE)) {
            const content = fs.readFileSync(LOCK_FILE, 'utf8').trim();
            if (parseInt(content, 10) === process.pid) {
                fs.unlinkSync(LOCK_FILE);
            }
        }
    } catch (err) {
        // ignore
    }
}

acquireProcessLock();

// An unhandled rejection is logged because it may be an isolated failed
// operation. An uncaught exception is different: process state can no longer
// be trusted, so exit and let the Python parent restart us from saved auth.
process.on('unhandledRejection', (err) => {
    console.error('Unhandled rejection (kept process alive):', err);
});
process.on('uncaughtException', (err) => {
    console.error('Fatal uncaught exception; exiting so the parent can restart the service:', err);
    if (shutdownStarted) return;
    shutdownStarted = true;
    void flushAuthWrites()
        .catch((flushError) => console.error('Could not flush WhatsApp auth writes after fatal error:', flushError))
        .finally(() => {
            releaseProcessLock();
            process.exit(1);
        });
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
            // Consume the response even though its body is not needed, so the
            // socket can close cleanly and the HTTP client does not retain it.
            res.resume();
        });

        req.on('error', (err) => console.error('Error sending QR to Telegram:', err));
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

// Attached to the "reconnected" alert; handled by studip_bot.py, which
// calls /send_test below.
const TEST_MESSAGE_KEYBOARD = { inline_keyboard: [[{ text: '🧪 Send WA Test Message', callback_data: 'wa_test_message' }]] };

let sock = null;
let isAuthenticated = false;
let isReconnecting = false;
let socketSetupPromise = null;
let flushAuthWrites = async () => {};
let shutdownStarted = false;
let reconnectFailures = 0; // consecutive closes since the last open, drives the reconnect backoff
let sessionLoggedOut = false; // true after WhatsApp rejected the saved session (logged out), so /request_qr knows reusing it is pointless
let hasEverConnected = false; // true once the connection has opened at least once this process - never reset, so /status can tell "waiting for the first-ever QR" apart from "was connected, session dropped"
let consecutiveConflictCount = 0;
let isConflictPaused = false;
let stableConnectionTimer = null;
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
// reusing the same auth folder (see auth_state.js) so a still-valid session
// restores silently with no new QR.
function connectToWhatsApp() {
    if (socketSetupPromise) return socketSetupPromise;
    let tracked;
    tracked = _connectToWhatsApp().finally(() => {
        if (socketSetupPromise === tracked) socketSetupPromise = null;
    });
    socketSetupPromise = tracked;
    return tracked;
}

async function _connectToWhatsApp() {
    const authState = await useResilientAuthState(AUTH_DIR, { onAlert: (text) => sendTextToTelegram(text) });
    flushAuthWrites = authState.flushWrites;
    const { state, saveCreds } = authState;

    sock = makeWASocket({
        auth: state,
        logger: pino({ level: 'silent' }),
    });

    sock.ev.on('creds.update', saveCreds);

    sock.ev.on('connection.update', (update) => {
        const { connection, lastDisconnect, qr } = update;

        if (qr) {
            // The QR payload grants a WhatsApp session pairing. Never print
            // it into persistent service logs; deliver it only to the
            // configured administrator through Telegram.
            console.log('WhatsApp pairing QR generated; sending it to the configured administrator.');
            sendQRToTelegram(qr);
        }

        if (connection === 'open') {
            console.log('WhatsApp Client is ready!');
            isAuthenticated = true;
            isReconnecting = false;
            sessionLoggedOut = false;
            hasEverConnected = true;

            // Clear any prior stability check
            if (stableConnectionTimer) {
                clearTimeout(stableConnectionTimer);
            }

            // Require 30 seconds of continuous uptime before resetting
            // failure/conflict counters and announcing reconnection.
            // This prevents "alert spam" and ping-pong wars during conflict storms.
            stableConnectionTimer = setTimeout(() => {
                reconnectFailures = 0;
                consecutiveConflictCount = 0;
                isConflictPaused = false;
                console.log('WhatsApp connection confirmed stable (30s+ uninterrupted).');
                if (disconnectAlertPending) {
                    disconnectAlertPending = false;
                    sendTextToTelegram('🟢 WhatsApp reconnected - forwarding is back up.', TEST_MESSAGE_KEYBOARD);
                }
            }, 30000);
        }

        if (connection === 'close') {
            isAuthenticated = false;
            if (stableConnectionTimer) {
                clearTimeout(stableConnectionTimer);
                stableConnectionTimer = null;
            }

            const statusCode = lastDisconnect?.error?.output?.statusCode;
            const reasonText = lastDisconnect?.error?.message || String(lastDisconnect?.error || 'unknown');
            const isConflict = statusCode === 440 || reasonText.toLowerCase().includes('conflict');

            if (statusCode === DisconnectReason.loggedOut) {
                // A real logout (force_new_qr, or the account was unlinked from
                // the phone) - the saved session is dead, auto-reconnecting
                // with it would just loop. Wait for an explicit /request_qr or
                // /force_new_qr instead of retrying on our own. The reconnect
                // that got here is over, so clear the guard: otherwise every
                // later QR request is skipped as "already in progress".
                isReconnecting = false;
                sessionLoggedOut = true;
                console.log('WhatsApp session logged out - not auto-reconnecting. Use /request_qr or /force_new_qr to relink.');
                sendTextToTelegram('🔴 WhatsApp logged this session out. Tap "📲 Request WA QR" in /status to link it again with a new QR code.');
                return;
            }

            // Whatever attempt produced this close is over, so the guard must
            // not keep blocking the next one (it used to stay set forever
            // when a close arrived before the connection ever opened).
            isReconnecting = false;

            if (!state.creds.me) {
                // Never paired: this is the QR running out, not an outage.
                // Retrying would just post QR codes to Telegram in a loop.
                console.log(`WhatsApp QR expired without a scan (${reasonText}).`);
                sendTextToTelegram('⌛ The WhatsApp QR code expired without a scan. Tap "📲 Request WA QR" in /status for a new one when your phone is at hand.');
                return;
            }

            console.log('WhatsApp client disconnected:', reasonText);

            if (isConflict) {
                consecutiveConflictCount += 1;
                console.warn(`WhatsApp session conflict detected (consecutive count: ${consecutiveConflictCount})`);

                // Circuit breaker: stop infinite ping-pong if another client is actively reconnecting
                if (consecutiveConflictCount >= 3) {
                    isConflictPaused = true;
                    console.error('Multiple consecutive session conflicts detected. Pausing auto-reconnect to break ping-pong loop.');
                    sendTextToTelegram('⚠️ WhatsApp session conflict detected: Another active session/device is competing for this connection. Auto-reconnect paused to prevent loop. Tap "📲 Request WA QR" in /status when ready.');
                    return;
                }
            }

            // Proactive alert instead of only showing up passively in /status -
            // an outage could otherwise go unnoticed for hours. Only the first
            // close event of an outage alerts (see disconnectAlertPending);
            // reconnectClient's own isReconnecting guard already prevents
            // duplicate reconnect attempts if 'close' fires more than once
            // while already down.
            if (!disconnectAlertPending) {
                disconnectAlertPending = true;
                sendTextToTelegram(`🔴 WhatsApp disconnected (${reasonText}) - attempting to reconnect automatically. Forwarding won't work until this recovers.`);
            }

            // Back off: conflicts start at 30 s; standard blips start at 5 s
            const baseDelay = isConflict ? 30000 : 5000;
            const delay = Math.min(baseDelay * 2 ** reconnectFailures, 5 * 60 * 1000);
            reconnectFailures += 1;
            console.log(`Scheduling reconnect attempt in ${Math.round(delay / 1000)} seconds...`);
            setTimeout(() => {
                if (!isAuthenticated && !isConflictPaused) {
                    reconnectClient(`disconnected: ${reasonText}`);
                }
            }, delay);
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
    // The initial startup connect is not a reconnect and previously did not
    // set isReconnecting. Wait for its socket/auth setup to finish before
    // tearing down the socket or deleting auth files for a forced QR flow.
    const pendingSetup = socketSetupPromise;
    if (pendingSetup) {
        try {
            await pendingSetup;
        } catch {
            // The normal reconnect path below retries a failed setup.
        }
        if (isReconnecting) return;
    }
    isReconnecting = true;
    isAuthenticated = false;
    console.log(`Re-initializing WhatsApp client (${reason})...`);

    // Cleanly tear down existing socket and listeners before creating a new one
    if (sock) {
        try {
            sock.ev.removeAllListeners();
            if (sock.ws) {
                sock.ws.close();
            }
        } catch (err) {
            // ignore
        }
    }

    // Event listeners are detached now, so this is the final batch of writes
    // owned by the old auth state. Finish it before either reloading the same
    // files into a new state instance or deleting them for a new QR code.
    await flushAuthWrites();

    if (forceLogout) {
        try {
            if (sock) await sock.logout();
        } catch (err) {
            console.error('Error logging out client:', err);
        }
        // logout() may trigger final Signal key removals or credential saves.
        // Wait for those before deleting the directory so no stale write can
        // recreate old session files after the reset.
        await flushAuthWrites();
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
    await flushAuthWrites();
    sock = null;

    try {
        await connectToWhatsApp();
    } catch (err) {
        console.error('Error re-initializing client:', err);
        isReconnecting = false;
        const errorText = err && err.message ? err.message : String(err);
        sendTextToTelegram(`🔴 WhatsApp reconnect attempt failed (${reason}): ${errorText}\n\nRetrying automatically with a backoff. You can also tap "📲 Request WA QR" in /status.`);
        const delay = Math.min(5000 * 2 ** reconnectFailures, 5 * 60 * 1000);
        reconnectFailures += 1;
        console.log(`Scheduling another reconnect attempt in ${Math.round(delay / 1000)} seconds...`);
        setTimeout(() => {
            if (!isAuthenticated && !isReconnecting && !sessionLoggedOut && !isConflictPaused) {
                reconnectClient(`retry after failed attempt: ${reason}`);
            }
        }, delay);
    }
}

app.post('/request_qr', async (req, res) => {
    // Reset circuit breakers when operator explicitly requests reconnection
    isConflictPaused = false;
    consecutiveConflictCount = 0;
    reconnectFailures = 0;

    if (sessionLoggedOut) {
        res.json({ status: 'waiting', message: 'The saved session was logged out by WhatsApp. A new QR code will be sent shortly.' });
        await reconnectClient('manual /request_qr after logout', { forceLogout: true });
        return;
    }
    res.json({ status: 'waiting', message: 'Reconnecting. If a saved session is still valid it will restore silently; otherwise a QR code will be sent shortly.' });
    await reconnectClient('manual /request_qr');
});

// Forces a brand new QR code even if currently authenticated, by invalidating
// the saved session first. Use this to link a different phone/account or when
// the connection is stuck in a way a plain reconnect can't fix.
app.post('/force_new_qr', async (req, res) => {
    isConflictPaused = false;
    consecutiveConflictCount = 0;
    reconnectFailures = 0;
    res.json({ status: 'waiting', message: 'Logging out and generating a brand new QR code. It will be sent to you shortly.' });
    await reconnectClient('manual /force_new_qr', { forceLogout: true });
});

// API endpoint to check authentication status.
app.get('/status', (req, res) => {
    res.json({ isAuthenticated, isReconnecting, hasEverConnected, isConflictPaused });
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
        if (isNotAGroupMember(err)) {
            // WhatsApp answers without a <group> node (or "item-not-found" /
            // "forbidden") for a group this account has left or never joined:
            // that is a normal answer, not a server failure.
            console.log(`Group ${groupId} is unknown to this WhatsApp account (left or never joined)`);
            return res.status(404).json({ error: 'Not a member of this group', notMember: true });
        }
        console.error('Error resolving group name:', err);
        res.status(500).json({ error: `Internal server error: ${err && err.message ? err.message : String(err)}` });
    }
});

// Baileys reports "this account is not in that group" in several ways
// depending on the server's reply; all of them mean the same to callers.
function isNotAGroupMember(err) {
    const message = String((err && err.message) || err || '').toLowerCase();
    const status = err && err.output && err.output.statusCode;
    return (
        message.includes('missing <group> node') ||
        message.includes('item-not-found') ||
        message.includes('forbidden') ||
        message.includes('not-authorized') ||
        status === 403 || status === 404
    );
}

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

// Sends a short test message to the linked account's own chat ("Message
// yourself"), never to a group, to confirm sending works end to end.
app.post('/send_test', async (req, res) => {
    if (!isAuthenticated || !sock?.user?.id) {
        return res.status(503).json({ error: 'WhatsApp client is not ready yet or needs a QR scan.' });
    }
    try {
        const ownJid = jidNormalizedUser(sock.user.id);
        const time = new Date().toLocaleString('de-DE', { timeZone: 'Europe/Berlin' });
        const sent = await sock.sendMessage(ownJid, { text: `✅ UniAssistant WhatsApp test message (${time}). Sending works.` });
        if (!sent) {
            return res.status(502).json({ error: 'WhatsApp did not confirm the message was sent.' });
        }
        console.log('Test message sent to own chat.');
        res.json({ success: true, to: `+${ownJid.split('@')[0]}` });
    } catch (err) {
        console.error('Error sending test message:', err);
        res.status(500).json({ error: err && err.message ? err.message : String(err) });
    }
});

// API endpoint to send a message
app.post('/send', async (req, res) => {
    if (!isAuthenticated) {
        return res.status(503).json({ error: 'WhatsApp client is not ready yet or needs a QR scan. Please wait 30 seconds or request a new QR code.' });
    }

    const { text, groupName } = req.body;

    if (typeof text !== 'string' || text.length === 0
        || typeof groupName !== 'string' || groupName.length === 0) {
        return res.status(400).json({ error: 'Missing text or groupName in request body' });
    }

    try {

        // Prefer an explicit JID (group or individual chat) over the process
        // environment. For older callers that send a display name, retain the
        // configured group as the fallback target.
        const requestedJid = typeof groupName === 'string'
            && (groupName.endsWith('@g.us') || groupName.endsWith('@c.us'))
            ? groupName
            : null;
        const targetId = requestedJid || process.env.WA_GROUP_ID;

        if (targetId) {
            // jidNormalizedUser is a no-op for group JIDs (@g.us has no
            // per-device suffix to strip) - this is purely defensive.
            const sent = await sock.sendMessage(jidNormalizedUser(targetId), { text });
            if (!sent) {
                noteSendFailureAndReconnect(targetId);
                return res.status(502).json({ error: 'WhatsApp accepted the request but did not confirm the message was sent (it may or may not have actually gone out - check the group before resending). Reconnecting in the background; try again in about 30 seconds.' });
            }
            const destination = targetId.endsWith('@c.us') ? 'direct ID' : 'group ID';
            console.log(`Message sent directly to ${destination}: ${targetId}`);
            return res.json({ success: true, message: `Message sent via ${destination}` });
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

// Graceful shutdown on exit (PM2 restart/stop)
const cleanupAndExit = async () => {
    if (shutdownStarted) return;
    shutdownStarted = true;
    console.log('\nShutting down WhatsApp service...');
    if (stableConnectionTimer) {
        clearTimeout(stableConnectionTimer);
        stableConnectionTimer = null;
    }
    try {
        if (sock) {
            sock.ev.removeAllListeners();
            if (sock.ws) sock.ws.close();
            sock.end(undefined);
        }
    } catch (err) {
        // Ignore errors during shutdown
    }
    await flushAuthWrites();
    releaseProcessLock();
    process.exit(0);
};

process.on('SIGINT', cleanupAndExit);
process.on('SIGTERM', cleanupAndExit);
process.on('SIGQUIT', cleanupAndExit);

const server = app.listen(PORT, HOST, () => {
    console.log(`WhatsApp service listening on ${HOST}:${PORT}`);
    // Start WhatsApp connection ONLY after the HTTP port is bound
    connectToWhatsApp().catch((err) => {
        console.error('Initial WhatsApp connection setup failed:', err);
        reconnectClient('initial connection setup failed');
    });
});

server.on('error', (err) => {
    if (err && err.code === 'EADDRINUSE') {
        console.error(`FATAL: Port ${PORT} is already in use by another process. Exiting immediately to prevent conflict loop.`);
        releaseProcessLock();
        process.exit(1);
    } else {
        console.error('Server listen error:', err);
        releaseProcessLock();
        process.exit(1);
    }
});
