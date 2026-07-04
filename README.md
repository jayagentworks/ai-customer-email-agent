# AI Customer Support Email Agent

An AI-powered customer support email agent that syncs real QQ mailbox messages, classifies support intent, retrieves knowledge-base evidence, drafts replies, and routes risky cases to a human review dashboard.

This is a portfolio-oriented full-stack agent project focused on practical customer-service automation rather than a toy chatbot.

## Features

- Real QQ mailbox integration through IMAP and SMTP.
- Asynchronous email import so the UI does not block while the Agent processes messages.
- LangGraph `StateGraph` agent workflow:
  - preprocessing
  - non-support email filtering
  - semantic classification
  - RAG retrieval
  - reply drafting
  - review decision
- Human-in-the-loop review queue for high-risk, low-confidence, refund, complaint, and escalation cases.
- Knowledge base management:
  - Markdown/PDF/DOCX/DOC upload
  - document parsing and cleaning
  - chunking and indexing
  - version history
  - rollback and deletion
  - strong and weak duplicate detection
- Hybrid RAG retrieval with pgvector semantic recall, keyword scoring, and category reranking.
- Chinese-first enterprise dashboard with optional English mode.
- Agent observability:
  - workflow trace
  - knowledge evidence
  - LLM call count
  - token estimate
  - RAG latency
  - per-email cost estimate
  - daily cost summary
- Local fallback mode when LLM or embedding API keys are not configured.

## Screens and Modules

- Inbox: sync and inspect incoming emails.
- Review Queue: approve, revise, escalate, undo escalation, regenerate drafts, and send replies.
- Knowledge Base: upload, edit, version, rollback, reindex, and inspect parsing reports.
- Run Logs: view email Agent traces and knowledge-base operation logs.
- Settings: language and workspace preferences.

## Architecture

```mermaid
flowchart LR
  QQ[QQ Mail] --> API[FastAPI backend]
  API --> DB[(PostgreSQL)]
  API --> Agent[LangGraph StateGraph]
  Agent --> Preprocess[Preprocess]
  Preprocess --> Gate[Relevance gate]
  Gate -->|non-support email| Filtered[No reply generated]
  Gate -->|support email| Semantic[Semantic analysis]
  Semantic --> RAG[RAG retrieval]
  RAG --> Draft[Reply draft]
  Draft --> Review[Human review]
  Review --> SMTP[QQ SMTP send]
  KB[Knowledge base] --> RAG
  UI[React dashboard] --> API
```

More details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## Tech Stack

- Backend: FastAPI, Python, SQLAlchemy, PostgreSQL
- Agent workflow: LangGraph StateGraph
- Frontend: React, TypeScript, Vite
- Mail: QQ IMAP/SMTP
- RAG: PostgreSQL + pgvector semantic recall, keyword scoring, category reranking
- Document parsing: PDF, DOCX, DOC, text-like files
- LLM/Embedding: OpenAI-compatible API, configured for SiliconFlow by default

## Quick Start

### Docker Deployment

Docker is the recommended way to run PostgreSQL + pgvector locally. The verified
defaults use official Docker images, while package installation uses domestic
mirrors where possible:

- PostgreSQL + pgvector image: `pgvector/pgvector:pg17`
- Python image: `python:3.12-slim`
- Node image: `node:22-alpine`
- Nginx image: `nginx:1.27-alpine`
- Python packages: Tsinghua PyPI mirror
- npm packages: npmmirror

If your Docker image mirror is stable, you can override the image sources:

```powershell
$env:PGVECTOR_IMAGE="docker.m.daocloud.io/pgvector/pgvector:pg17"
$env:PYTHON_IMAGE="docker.m.daocloud.io/library/python:3.12-slim"
$env:NODE_IMAGE="docker.m.daocloud.io/library/node:22-alpine"
$env:NGINX_IMAGE="docker.m.daocloud.io/library/nginx:1.27-alpine"
docker compose up -d --build
```

The backend image also installs local OCR/runtime dependencies:

- `tesseract-ocr`
- `tesseract-ocr-chi-sim`
- `tesseract-ocr-eng`
- `fonts-noto-cjk`
- `libreoffice-writer`
- `poppler-utils`

PDF parsing uses a layered strategy: the system first extracts the built-in PDF
text layer with `pypdf`; if a page has no extractable text, it renders that page
with `pdftoppm` and runs local `tesseract` OCR with `chi_sim+eng`. This keeps
normal text PDFs fast while still supporting scanned Chinese/English PDFs.

PDF tables are extracted with `pdfplumber` and indexed as Markdown table chunks,
so row/column relationships are preserved better than plain text extraction.
Embedded PDF images are saved under `backend/knowledge_docs/extracted_assets/`
and indexed as image-reference chunks with page numbers and file paths. The
current text RAG can retrieve these references, but it does not semantically
understand non-text images unless OCR text or a future multimodal image model is
added.

If `SILICONFLOW_API_KEY` is configured, extracted PDF images are also sent to the
optional vision model `Qwen/Qwen3-VL-8B-Instruct` to generate a concise Chinese
image description. The description is indexed as text in the same image-reference
chunk, so diagrams, screenshots, and product images can participate in RAG
retrieval. If the vision API is unavailable, the system keeps the image asset
reference and continues indexing without blocking the upload.

Start all services:

```powershell
copy .env.example .env
# Edit .env and configure QQ mailbox and LLM/embedding API keys.
docker compose up -d --build
```

Open:

```text
http://127.0.0.1:5173
```

Backend health check:

```powershell
Invoke-RestMethod http://127.0.0.1:8010/
```

If you only want the database container and still run backend/frontend on the
host machine:

```powershell
docker compose up -d db
```

Then keep this local database URL:

```env
DATABASE_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:5433/customer_email_agent
```

### 1. Backend

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r backend\requirements.txt
copy .env.example .env
```

Edit `.env` and configure at least:

```env
DATABASE_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:5433/customer_email_agent
QQ_EMAIL_ADDRESS=your_qq_email@qq.com
QQ_EMAIL_AUTH_CODE=your_qq_mail_auth_code
SILICONFLOW_API_KEY=your_siliconflow_api_key
```

Then run:

```powershell
cd backend
..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8010
```

If you run the command from the repository root instead:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8010
```

### 2. Frontend

```powershell
cd frontend
npm install
npm run dev
```

Open:

```text
http://127.0.0.1:5173
```

## Environment Variables

See [.env.example](.env.example).

Important variables:

- `SILICONFLOW_API_KEY`: API key for SiliconFlow.
- `LLM_BASE_URL`: OpenAI-compatible chat completion endpoint.
- `LLM_MODEL`: default `deepseek-ai/DeepSeek-V4-Flash`.
- `VISION_MODEL`: default `Qwen/Qwen3-VL-8B-Instruct` for PDF image descriptions.
- `EMBEDDING_BASE_URL`: OpenAI-compatible embedding endpoint.
- `EMBEDDING_MODEL`: default `Qwen/Qwen3-Embedding-0.6B`.
- `QQ_EMAIL_ADDRESS`: QQ mailbox address.
- `QQ_EMAIL_AUTH_CODE`: QQ mailbox authorization code, not the login password.
- `DATABASE_URL`: SQLAlchemy database URL.

When API keys are missing, the system falls back to local rule-based classification and local hash embeddings.

For PostgreSQL deployments, enable pgvector in the target database:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

The app stores embeddings in both JSON form and an optional `embedding_vector`
pgvector column. If pgvector is unavailable, retrieval falls back to Python
cosine similarity over the JSON embeddings.

## Validation

Frontend:

```powershell
cd frontend
npm run build
```

Backend:

```powershell
.\.venv\Scripts\python.exe -m compileall backend\app
```

## Resume Notes

See [docs/RESUME_NOTES.md](docs/RESUME_NOTES.md).

## Security

Do not commit:

- `.env`
- mailbox authorization codes
- API keys
- local databases
- uploaded private knowledge files
- generated build outputs

The repository includes `.env.example` for safe configuration sharing.

## License

MIT
