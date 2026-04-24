# DB Module Map

This project now uses a `db/` package.

- `db/__init__.py`
: Compatibility facade. Existing `import db` still works.
- `db/schema.py`
: Schema/bootstrap initialization (`init_db`).
- `db/connection.py`
: Connection/bootstrap helpers (`init_db`, `_connect`, `_now_kst_naive`).
- `db/audit.py`
: Audit/message logs (migrated implementation).
- `db/ticket.py`
: Ticket helpers (migrated implementation).
- `db/problem.py`
: Problem and bonus-problem helpers.
- `db/team.py`
: Team and user score helpers (migrated implementation).
- `db/cguard.py`
: C-GUARD status helpers (migrated implementation).

Recommended import style for new code:

```python
from db import team as db_team
from db import problem as db_problem
```
