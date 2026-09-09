require('dotenv').config({ path: '../.env' });
const express = require('express');
const { Client, LocalAuth } = require('whatsapp-web.js');
const qrcodeTerminal = require('qrcode-terminal');
const qrcode = require('qrcode');
const FormData = require('form-data');
const https = require('https');

const app = express();
app.use(express.json());

const PORT = process.env.PORT || 3838;
const TELEGRAM_TOKEN = process.env.TELEGRAM_TOKEN;
const ALLOWED_USER_IDS = process.env.ALLOWED_USER_IDS ? process.env.ALLOWED_USER_IDS.split(',') : [];

// Safety net: whatsapp-web.js/Puppeteer occasionally throws from internal async
// code paths that aren't awaited by our own try/catch blocks (e.g. duplicate
// page-binding errors during a fast destroy+initialize cycle). Without these
// handlers such an error is an unhandled rejection/exception and Node exits,
// killing the whole service (and pm2 then restarts it from scratch, dropping
// the session). Log and keep running instead; reconnectClient()'s own guard
// (isReconnecting) prevents pile-ups if this fires during a reconnect.
process.on('unhandledRejection', (err) => {
    console.error('Unhandled rejection (kept process alive):', err);
});
process.on('uncaughtException', (err) => {
    console.error('Uncaught exception (kept process alive):', err);
});

// Initialize WhatsApp Client
const client = new Client({
    authStrategy: new LocalAuth(),
    // Puppeteer options for running smoothly on various environments
    puppeteer: {
        args: ['--no-sandbox', '--disable-setuid-sandbox']
    }
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

let lastQrCode = null;
let isAuthenticated = false;
let isReconnecting = false;
const announcedGroupIds = new Set();
const discoveredGroups = new Map(); // groupId -> { name, lastSeen }

client.on('qr', (qr) => {
    lastQrCode = qr;
    // Generate and scan this code with your phone
    console.log('QR Code received, scan please:');
    qrcodeTerminal.generate(qr, { small: true });
    
    // Also send to Telegram
    sendQRToTelegram(qr);
});

client.on('ready', () => {
    console.log('WhatsApp Client is ready!');
    isAuthenticated = true;
    isReconnecting = false;
    lastQrCode = null;
});

// Destroys and re-initializes the client. Guarded against overlapping calls so a
// disconnect event, a failed send, and a manual /request_qr can't race each other.
//
// If forceLogout is true, the stored LocalAuth session is invalidated first
// (via client.logout()) so initialize() is forced to emit a real 'qr' event
// instead of silently restoring the existing session - use this only when the
// caller explicitly wants a fresh QR (e.g. to link a different phone), since
// it breaks the current connection and requires re-scanning.
async function reconnectClient(reason, { forceLogout = false } = {}) {
    if (isReconnecting) {
        console.log(`Reconnect already in progress, skipping duplicate trigger (${reason}).`);
        return;
    }
    isReconnecting = true;
    isAuthenticated = false;
    lastQrCode = null;
    console.log(`Re-initializing WhatsApp client (${reason})...`);
    if (forceLogout) {
        try {
            await client.logout();
        } catch (err) {
            console.error('Error logging out client:', err);
        }
    }
    try {
        await client.destroy();
    } catch (err) {
        console.error('Error destroying client:', err);
    }
    // Give Puppeteer/Chromium time to fully tear down the old page before a new
    // one is created - re-initializing too fast makes whatsapp-web.js try to
    // expose its page bindings (e.g. onQRChangedEvent) on a window that still
    // has them from the old page, which throws and can crash the process.
    await new Promise(resolve => setTimeout(resolve, 3000));
    try {
        await client.initialize();
    } catch (err) {
        console.error('Error re-initializing client:', err);
        isReconnecting = false;
    }
}

client.on('disconnected', (reason) => {
    console.log('WhatsApp client disconnected:', reason);
    reconnectClient(`disconnected: ${reason}`);
});

app.post('/request_qr', async (req, res) => {
    // Always force a fresh QR on explicit request, even if isAuthenticated is
    // (possibly stale) true - the operator only calls this when the connection
    // is actually broken, so trust the request over a stale in-memory flag.
    // NOTE: if a valid session is still saved on disk (LocalAuth), this just
    // silently restores it and no 'qr' event fires - use /force_new_qr to
    // guarantee a real QR even when currently connected.
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

client.on('auth_failure', msg => {
    console.error('AUTHENTICATION FAILURE', msg);
});

client.initialize();

// API endpoint to check authentication status
app.get('/status', (req, res) => {
    res.json({ isAuthenticated });
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

// Helper to find a group by name
async function findGroupByName(name) {
    const chats = await client.getChats();
    const groups = chats.filter(chat => chat.isGroup);
    return groups.find(group => group.name === name);
}

// Log incoming AND outgoing messages so the user can easily discover Group IDs
// message_create fires even when you send a message from your own phone!
client.on('message_create', async msg => {
    try {
        // If it's a group message, msg.to (if sent by you) or msg.from (if received) is the Group ID
        const groupId = msg.fromMe ? msg.to : msg.from;
        
        if (groupId.endsWith('@g.us')) {
            console.log(`[GROUP ID DISCOVERY] Detected a message in a group! The Group ID is: ${groupId}`);

            // Always keep track of seen groups so they can be listed/set on demand later,
            // even after WA_GROUP_ID is already configured.
            let name = discoveredGroups.get(groupId)?.name;
            try {
                const chat = await msg.getChat();
                if (chat && chat.name) name = chat.name;
            } catch (e) {
                // best-effort only; getChat() can hit the same puppeteer flakiness as getChats()
            }
            discoveredGroups.set(groupId, { name, lastSeen: Date.now() });

            // Auto-notify admin via Telegram so WA_GROUP_ID can be set without relying on getChats()
            if (!process.env.WA_GROUP_ID && !announcedGroupIds.has(groupId)) {
                announcedGroupIds.add(groupId);
                sendTextToTelegram(
                    `📍 WhatsApp Group ID detected:\n${groupId}\n\nTap below to set it as the active group.`,
                    { inline_keyboard: [[{ text: '✅ Set as active group', callback_data: `set_wa_group|${groupId}` }]] }
                );
            }
        }
    } catch (e) {
        console.error("Error in message listener:", e);
    }
});

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

        // Use hardcoded WA_GROUP_ID from .env if available to completely bypass getChats()
        const hardcodedGroupId = process.env.WA_GROUP_ID;
        
        if (hardcodedGroupId) {
            await client.sendMessage(hardcodedGroupId, text);
            console.log(`Message sent directly to WA_GROUP_ID: ${hardcodedGroupId}`);
            return res.json({ success: true, message: 'Message sent via Hardcoded ID' });
        }

        // If the python bot sent an ID instead of a name (ends with @g.us or @c.us), use it directly
        if (groupName.endsWith('@g.us') || groupName.endsWith('@c.us')) {
            await client.sendMessage(groupName, text);
            console.log(`Message sent directly to ID: ${groupName}`);
            return res.json({ success: true, message: 'Message sent via direct ID' });
        }
        
        // Otherwise try searching by name
        const group = await findGroupByName(groupName);
        if (group) {
            await client.sendMessage(group.id._serialized, text);
            console.log(`Message sent to group "${groupName}"`);
            res.json({ success: true, message: 'Message sent' });
        } else {
            console.log(`Group "${groupName}" not found`);
            res.status(404).json({ error: 'Group not found' });
        }
    } catch (err) {
        console.error('Error sending message:', err);
        
        // Self-healing for Puppeteer evaluation errors (like "r" or Context lost)
        const errorString = err && err.message ? err.message : String(err);
        if (errorString === 'r' || errorString.includes('r: r') || errorString.includes('Evaluation failed') || errorString.includes('Session closed')) {
            console.log("WhatsApp Web client seems broken. Triggering self-healing restart...");
            reconnectClient(`send failure: ${errorString}`);
            return res.status(500).json({ error: 'WhatsApp client error. Re-initializing automatically. Please try again in 30 seconds.' });
        }
        
        res.status(500).json({ error: `Internal server error: ${errorString}` });

    }
});

app.listen(PORT, () => {
    console.log(`WhatsApp service listening on port ${PORT}`);
});

// Graceful shutdown: Clean up Puppeteer (Chrome) on exit (PM2 restart/stop)
const cleanupAndExit = async () => {
    console.log('\nShutting down WhatsApp service... Cleaning up browser instances.');
    try {
        await client.destroy();
    } catch (err) {
        // Ignore errors during shutdown
    }
    process.exit(0);
};

process.on('SIGINT', cleanupAndExit);
process.on('SIGTERM', cleanupAndExit);
process.on('SIGQUIT', cleanupAndExit);
