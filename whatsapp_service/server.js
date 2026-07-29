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
    lastQrCode = null;
});

client.on('disconnected', () => {
    isAuthenticated = false;
});

app.post('/request_qr', async (req, res) => {
    if (isAuthenticated) {
        return res.json({ status: 'authenticated', message: 'Already authenticated.' });
    }
    
    // YENİ QR kodu üretmesi için client'i yeniden başlatıyoruz.
    res.json({ status: 'waiting', message: 'Generating a fresh QR code. It will be sent to you shortly.' });
    
    try {
        await client.destroy();
    } catch (err) {
        console.error('Error destroying client:', err);
    }
    
    lastQrCode = null;
    console.log('Re-initializing client to get a fresh QR code...');
    client.initialize();
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
            isAuthenticated = false;
            client.destroy().catch(() => {}).then(() => client.initialize());
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
