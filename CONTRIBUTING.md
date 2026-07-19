# Участие в разработке

Изменения, затрагивающие оплату, eligibility, rollback или compatibility,
принимаются только вместе с тестами.

Перед pull request:

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
.venv/bin/pytest -q
.venv/bin/python scripts/verify_compatibility.py
```

Не добавляйте реальные UUID, IP, токены, дампы БД и `.env`.
