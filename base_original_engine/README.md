# Outreach Agent AI

An autonomous, intent-first B2B prospecting and cold outreach engine powered by multi-LLM cascades, deep contact intelligence, multi-channel Gmail dispatch, and a real-time web dashboard.

---

## Architecture & Funnel Pipeline

```text
+-----------------------------------------------------------------------------------+
| 1. SIGNAL DISCOVERY                                                               |
|    DuckDuckGo search queries detecting real buying signals & intent indicators    |
+------------------------------------------+----------------------------------------+
                                           |
                                           v
+-----------------------------------------------------------------------------------+
| 2. DEEP WEBSITE RESEARCH & FILTERING                                              |
|    Multi-page domain crawler + keyword/domain blacklist + e-commerce validation  |
+------------------------------------------+----------------------------------------+
                                           |
                                           v
+-----------------------------------------------------------------------------------+
| 3. CONTACT INTELLIGENCE                                                           |
|    Public decision-maker discovery (Founders/CEOs/CMOs) + MX record verification  |
|    * Zero fabricated email guesses; strictly observable public evidence           |
+------------------------------------------+----------------------------------------+
                                           |
                                           v
+-----------------------------------------------------------------------------------+
| 4. MULTI-LLM QUALIFICATION                                                        |
|    Schema-validated qualification evaluating observable evidence vs. offer fit     |
|    Failover cascade: NVIDIA NIM  ->  OpenRouter  ->  Gemini                      |
+------------------------------------------+----------------------------------------+
                                           |
                                           v
+-----------------------------------------------------------------------------------+
| 5. EVIDENCE-BASED PERSONALIZATION                                                 |
|    Hyper-personalized, concise email copy (no templates, no bracketed fillers)     |
+------------------------------------------+----------------------------------------+
                                           |
                                           v
+-----------------------------------------------------------------------------------+
| 6. MULTI-CHANNEL DISPATCH & REPORTING                                             |
|    Gmail OAuth API (Drafts / Send) + Multi-account routing + Run Reports (JSON/MD)|
+-----------------------------------------------------------------------------------+
```

---

## Key Features

- **Multi-LLM Failover Cascade**: Single unified OpenAI-compatible client interfacing NVIDIA NIM (`Llama 3.3 Nemotron Super`), OpenRouter, and Google Gemini (`gemini-3.8-flash`). Automatic schema validation ensures that rate limits, timeouts, or malformed JSON instantly trigger graceful fallback.
- **Contact Intelligence**: Identifies verifiable public executives, parses role seniority, verifies DNS MX records, and matches named decision-makers with corporate email addresses.
- **Strict Anti-Hallucination & Anti-Spam Guardrails**:
  - Pacing delays between dispatches to protect sender reputation.
  - Emergency Kill-Switch (`emergency_stop: true`) to instantly freeze all outreach.
  - Dynamic Blacklist filtering by domain, TLD, keyword, or industry.
  - Unsubscribe / opt-out footer compliance.
  - Safe default mode (`draft`) allowing human-in-the-loop review before sending.
- **Interactive Web Dashboard**: Built with FastAPI and real-time frontend UI:
  - Live metric widgets (pipeline funnel, AI qualifications, verified decision-makers).
  - Searchable and filterable lead database with full research details and drafted emails.
  - Real-time live execution logs stream.
  - One-click trigger buttons for Discovery, Outreach, and Full Pipeline runs.
  - Visual run history with formatted Markdown & JSON inspection.
- **Audit & Run Reporting**: Automatically exports structured execution logs (`reports/*.json` and `reports/*.md`) summarizing scans, conversion rates, and exact AI rejection rationales.

---

## Project Structure

```text
OutreachAgent/
├── ai_agent.py          # Multi-LLM provider adapter, qualification & email copy generation
├── contact_agent.py     # Decision-maker discovery, role ranking & DNS MX validator
├── main.py              # Crawler core, DuckDuckGo scraper, Gmail API authentication
├── smart_main.py        # Orchestration engine & CLI interface
├── server.py            # FastAPI REST backend for web dashboard & background jobs
├── reporter.py          # Structured run reporting (Markdown & JSON generator)
├── static/
│   └── index.html       # Single-page dashboard application (dark-themed UI)
├── reports/             # Execution summary reports directory
├── start_dashboard.bat  # One-click Windows dashboard launcher
├── config.example.yaml  # Modular offer, signals, ICP & channels configuration
├── .env.example         # Environment variables & API keys template
├── requirements.txt     # Python package dependencies
└── README.md            # Comprehensive documentation
```

---

## Quickstart & Setup

### 1. Prerequisites
- Python 3.10+
- Google Cloud project with Gmail API enabled (for OAuth desktop credentials)

### 2. Installation

Clone the repository and set up a virtual environment:

```bash
git clone https://github.com/aigentix-ai/outreach-agent-ai.git
cd outreach-agent-ai

# Create virtual environment
python -m venv .venv

# Activate virtual environment
# Windows (PowerShell):
.\.venv\Scripts\Activate.ps1
# Windows (CMD):
.\.venv\Scripts\activate.bat
# Linux / macOS:
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Configuration

Copy the example configuration and environment files:

```bash
# Windows:
copy config.example.yaml config.yaml
copy .env.example .env

# Linux / macOS:
cp config.example.yaml config.yaml
cp .env.example .env
```

#### Configure API Keys (`.env`)
Provide at least one LLM provider key:

```env
# NVIDIA NIM
NVIDIA_API_KEY=your_nvidia_api_key
NVIDIA_MODEL=nvidia/llama-3.3-nemotron-super-49b-v1.5

# OpenRouter
OPENROUTER_API_KEY=your_openrouter_api_key
OPENROUTER_MODEL=openrouter/free

# Google Gemini
GEMINI_API_KEY=your_gemini_api_key
GEMINI_MODEL=gemini-3.8-flash

# Gmail OAuth Credential Paths
GMAIL_CREDENTIALS_FILE=credentials.json
GMAIL_TOKEN_FILE=token.json
```

#### Customize Offer & ICP (`config.yaml`)
Tailor the offer, target industries, query search patterns, sending limits, and provider ordering inside `config.yaml`.

---

## Gmail API Authentication

1. Open [Google Cloud Console](https://console.cloud.google.com/) and enable the **Gmail API**.
2. Create an **OAuth 2.0 Client ID** configured as a **Desktop app**.
3. Download the credentials JSON and save it in the project root as `credentials.json`.
4. Run the one-time authentication flow:

```bash
python smart_main.py --gmail-auth
```

A browser window will open asking for permission to create drafts/messages. After authorizing, `token.json` is generated locally.

---

## Usage

### Option A: Web Dashboard (Recommended)

Launch the interactive dashboard:

```bash
# Windows one-click:
start_dashboard.bat

# Or via command line:
python -m uvicorn server:app --host 127.0.0.1 --port 8000
```

Navigate to `http://127.0.0.1:8000` to monitor stats, trigger pipeline actions, edit configurations, inspect leads, and read run reports.

### Option B: CLI Commands

#### 1. Discover, Research & Qualify Leads
```bash
python smart_main.py --discover
```
Searches public buying signals, crawls prospective websites, identifies decision-makers, checks MX validity, and qualifies prospects using AI.

#### 2. Generate Personalized Outreach (Drafts or Sends)
```bash
python smart_main.py --outreach
```
Selects top-scoring qualified leads, crafts personalized email messages, and creates Gmail drafts (or sends directly if configured).

#### 3. Run Full Pipeline End-to-End
```bash
python smart_main.py --all
```

#### 4. View Database Pipeline Statistics
```bash
python smart_main.py --stats
```

#### 5. Multi-Account Channel Dispatch
Specify which account from `config.yaml` to use:
```bash
python smart_main.py --outreach --channel secondary_gmail
```

---

## Security & Privacy Guidelines

- **Never commit secrets**: `.env`, `credentials.json`, `token.json`, and `leads.db` are explicitly ignored in `.gitignore`.
- **Default Draft Mode**: Always test and review newly generated emails in Gmail Drafts (`send_mode: draft`) before switching to live delivery (`send_mode: send`).
- **Emergency Halt**: If you need to stop dispatches immediately across all channels, toggle `emergency_stop: true` in `config.yaml` or directly from the Web Dashboard.

---

## License

MIT License. Designed and maintained for modern, high-intent automated prospecting.
