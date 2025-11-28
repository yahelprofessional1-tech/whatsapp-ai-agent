# AI-Powered WhatsApp Business Agent

An automated customer service agent designed to convert missed calls into leads using Generative AI. 
This system acts as a middleware between telephony infrastructure (Twilio) and LLM inference (Google Gemini), providing 24/7 autonomous customer engagement in Hebrew.

## 🚀 Key Features
* **Event-Driven Architecture:** Uses Webhooks to intercept `no-answer` call events in real-time.
* **Omnichannel Fallback:** Automatically triggers a WhatsApp session immediately after a missed voice call.
* **Context-Aware AI:** Utilizes Google Gemini 2.0 (LLM) with a custom system prompt ("Persona") to handle business queries (hours, stock, pricing) naturally.
* **Security:** API Keys and sensitive credentials are managed via Environment Variables (not hardcoded).

## 🛠️ Tech Stack
* **Language:** Python 3.10
* **Framework:** Flask (Microservice Architecture)
* **Telephony API:** Twilio (Voice & WhatsApp Messaging)
* **AI/LLM:** Google Gemini 2.0 Flash
* **Tunneling:** Ngrok (for local webhook development)

## ⚙️ How It Works
1.  **Trigger:** A customer calls the business number.
2.  **Interception:** If the call is missed/busy, Twilio posts a payload to the `/incoming` endpoint.
3.  **Engagement:** The system terminates the call and initiates a WhatsApp session via the `/status` endpoint.
4.  **Conversation:** Customer replies are processed by the `/whatsapp` endpoint, sent to the LLM for context processing, and the response is routed back via Twilio.

## 📦 Setup & Installation

1.  **Clone the repository:**
    ```bash
    git clone [https://github.com/YOUR_USERNAME/whatsapp-ai-agent.git](https://github.com/YOUR_USERNAME/whatsapp-ai-agent.git)
    ```

2.  **Install dependencies:**
    ```bash
    pip install flask twilio google-generativeai
    ```

3.  **Configure Environment:**
    Create a `.env` file with your credentials (SID, Token, API Keys).

## 🔌 Network Configuration

Since this application runs on a local Flask server, it requires a secure tunnel to expose the Webhook endpoints to Twilio.

1.  **Start Ngrok Tunnel:**
    Forward port 5000 to the public internet:
    ```bash
    ngrok http 5000
    ```

2.  **Configure Twilio Webhooks:**
    * **Voice URL:** Set to `https://<your-ngrok-url>/incoming` (HTTP POST)
    * **Messaging URL:** Set to `https://<your-ngrok-url>/whatsapp` (HTTP POST)

3.  **Run the Server:**
    ```bash
    python bot.py
    ```

    *Developed by [YAHEL BOARON] - 2025*