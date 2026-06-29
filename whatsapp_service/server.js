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

let lastQrCode = null;
let isAuthenticated = false;

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
    if (lastQrCode) {
        await sendQRToTelegram(lastQrCode);
        return res.json({ status: 'sent', message: 'QR code sent to Telegram again.' });
    }
    return res.status(400).json({ status: 'waiting', message: 'QR code not generated yet, please wait.' });
});

client.on('auth_failure', msg => {
    console.error('AUTHENTICATION FAILURE', msg);
});

client.initialize();

// Helper to find a group by name
async function findGroupByName(name) {
    const chats = await client.getChats();
    const groups = chats.filter(chat => chat.isGroup);
    return groups.find(group => group.name === name);
}

// API endpoint to send a message
app.post('/send', async (req, res) => {
    const { text, groupName } = req.body;

    if (!text || !groupName) {
        return res.status(400).json({ error: 'Missing text or groupName in request body' });
    }

    try {
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
        res.status(500).json({ error: 'Internal server error' });
    }
});

app.listen(PORT, () => {
    console.log(`WhatsApp service listening on port ${PORT}`);
});
