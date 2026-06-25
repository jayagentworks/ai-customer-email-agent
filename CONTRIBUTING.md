# Contributing

This project uses a small, practical workflow:

1. Create a focused branch for one feature or fix.
2. Keep backend and frontend changes scoped to the feature.
3. Run validation before committing:

```bash
cd frontend
npm run build
```

```bash
cd ..
.venv\Scripts\python.exe -m compileall backend\app
```

4. Use clear commit messages, preferably Conventional Commits:

```text
feat: add async QQ mail import
fix: prevent non-support emails entering review queue
docs: expand setup instructions
```

Do not commit `.env`, local databases, virtual environments, generated builds, or uploaded private knowledge files.
