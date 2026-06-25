# Resume Notes

## Project Title

AI Customer Support Email Agent

## One-line Summary

Built an AI agent system that syncs real QQ mailbox messages, classifies support intent, retrieves knowledge-base evidence, drafts replies, and routes risky cases to a human review dashboard.

## Resume Bullets

- Built a FastAPI + React email agent that integrates QQ IMAP/SMTP, asynchronous background processing, and a human-in-the-loop review queue.
- Implemented a LangGraph workflow for preprocessing, semantic classification, non-support filtering, RAG retrieval, reply drafting, and review decisions.
- Designed a hybrid RAG system with document upload, PDF/DOCX/DOC parsing, chunking, version rollback, duplicate detection, and source-level retrieval evidence.
- Added cost observability including per-email token estimates, LLM call counts, RAG latency, single-run cost estimates, and daily cost summaries.
- Improved production usability with async mailbox sync, Chinese/English UI switching, run-log retention, and filtering for platform/security notification emails.

## Suggested Tech Stack Line

FastAPI, React, TypeScript, LangGraph, PostgreSQL, SQLAlchemy, QQ IMAP/SMTP, SiliconFlow/OpenAI-compatible LLM API, RAG, PDF/DOCX parsing.
