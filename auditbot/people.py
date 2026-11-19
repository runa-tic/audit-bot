"""Who may use the bot: an access-code model, where a person asks and a root approves them by the code they read out.

A person taps /start; the update carries their Telegram id, so nobody types an id. They land as
`pending` and the bot gives them a join code. They hand the code to the operator themselves (in
person or in a chat they already share), and the operator approves them BY NAME with
`/approve <code> <their name>`. The code proves the request is the person in front of you; the root
notification never shows it, so a stranger's /start can't be approved by a misread id.

Root is a numeric id (AUDIT_ROOT_TG_IDS) or a person promoted with a one-time claim code minted on
the server (`python -m auditbot.admin root-code`). Never a username.
"""
import secrets

from . import config, db

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I
CODE_LEN = 8


class PeopleError(Exception):
    pass


def norm_code(code):
    return "".join(ch for ch in (code or "").upper() if ch.isalnum())


def pretty(code):
    return f"{code[:4]}-{code[4:]}"


def _draw():
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))


def get(tg_id):
    r = db.one("SELECT * FROM people WHERE tg_id = ?", str(tg_id))
    return dict(r) if r else None


def is_root(tg_id):
    tg_id = str(tg_id)
    if tg_id in config.ROOT_TG_IDS:
        return True
    p = get(tg_id)
    return bool(p and p["status"] == "approved" and p["role"] == "root")


def may_use(tg_id):
    p = get(tg_id)
    return is_root(tg_id) or bool(p and p["status"] == "approved")


def roots():
    ids = set(config.ROOT_TG_IDS)
    ids |= {r["tg_id"] for r in db.q("SELECT tg_id FROM people WHERE status='approved' AND role='root'")}
    return sorted(ids)


def name_of(tg_id):
    p = get(tg_id)
    return (p and (p["name"] or p["asked_name"] or (p["username"] and "@" + p["username"]))) or str(tg_id)


def request(tg_id, username, asked_name):
    """-> (status, code, new). A pending request keeps its code; a decided one is not reopened here."""
    tg_id = str(tg_id)
    with db.tx():
        p = get(tg_id)
        if p and p["status"] in ("approved", "denied", "revoked"):
            return p["status"], None, False
        if p and p["status"] == "pending" and p["join_code"]:
            return "pending", p["join_code"], False
        for _ in range(50):
            code = _draw()
            if not db.one("SELECT 1 FROM people WHERE join_code = ?", code):
                break
        db.run("""INSERT INTO people (tg_id, username, asked_name, status, role, join_code, created_at)
                  VALUES (?,?,?,'pending','member',?,?)
                  ON CONFLICT(tg_id) DO UPDATE SET status='pending', join_code=excluded.join_code,
                  username=excluded.username, asked_name=excluded.asked_name""",
               tg_id, username or "", asked_name or "", code, db.now())
        return "pending", code, True


def approve(code, name, by):
    c = norm_code(code)
    name = " ".join((name or "").split())
    if len(c) != CODE_LEN:
        raise PeopleError("That's not a code: it's 8 letters and digits, and the person got it from the bot.")
    if not name:
        raise PeopleError("Name them: /approve <code> <their name>.")
    with db.tx():
        p = db.one("SELECT * FROM people WHERE join_code = ? AND status = 'pending'", c)
        if not p:
            raise PeopleError("No waiting request has that code. Ask them to send /start again and read you the code.")
        db.run("UPDATE people SET status='approved', name=?, join_code=NULL, decided_at=?, decided_by=? WHERE tg_id=?",
               name[:80], db.now(), str(by), p["tg_id"])
        return dict(p) | {"name": name}


def deny(code, by):
    c = norm_code(code)
    with db.tx():
        p = db.one("SELECT * FROM people WHERE join_code = ? AND status = 'pending'", c)
        if not p:
            raise PeopleError("No waiting request has that code.")
        db.run("UPDATE people SET status='denied', join_code=NULL, decided_at=?, decided_by=? WHERE tg_id=?",
               db.now(), str(by), p["tg_id"])
        return dict(p)


def revoke(tg_id, by):
    tg_id = str(tg_id).strip()
    if tg_id in config.ROOT_TG_IDS:
        raise PeopleError("That id is root from the server's env; it changes there, not here.")
    with db.tx():
        p = get(tg_id)
        if not p or p["status"] != "approved":
            raise PeopleError("No approved person has that id (see /people).")
        db.run("UPDATE people SET status='revoked', role='member', decided_at=?, decided_by=? WHERE tg_id=?",
               db.now(), str(by), tg_id)
        db.run("UPDATE watches SET status='cancelled', note='owner revoked' WHERE owner=? AND status='collecting'", tg_id)
        return p


def reopen(tg_id, by):
    """A denied or revoked person may ask again: they get a fresh code on their next /start."""
    with db.tx():
        p = get(tg_id)
        if not p or p["status"] not in ("denied", "revoked"):
            raise PeopleError("Only a denied or revoked person can be reopened.")
        db.run("UPDATE people SET status='pending', join_code=NULL, decided_by=? WHERE tg_id=?", str(by), str(tg_id))


def pending():
    return [dict(r) for r in db.q("SELECT * FROM people WHERE status='pending' ORDER BY created_at")]


def members():
    return [dict(r) for r in db.q("SELECT * FROM people WHERE status='approved' ORDER BY role DESC, name")]


def mint_root_code():
    code = _draw()
    db.run("INSERT INTO root_codes (code, created_at) VALUES (?,?)", code, db.now())
    return code


def claim_root(code, tg_id, username, asked_name):
    c = norm_code(code)
    with db.tx():
        r = db.one("SELECT * FROM root_codes WHERE code = ? AND used_by IS NULL AND created_at > ?", c, db.now() - 86400)
        if not r:
            raise PeopleError("That claim code is unknown, used, or older than a day.")
        db.run("UPDATE root_codes SET used_by = ? WHERE code = ?", str(tg_id), c)
        db.run("""INSERT INTO people (tg_id, username, asked_name, name, status, role, created_at, decided_at, decided_by)
                  VALUES (?,?,?,?,'approved','root',?,?,'claim-code')
                  ON CONFLICT(tg_id) DO UPDATE SET status='approved', role='root', join_code=NULL,
                  decided_at=excluded.decided_at, decided_by='claim-code'""",
               str(tg_id), username or "", asked_name or "", asked_name or username or str(tg_id), db.now(), db.now())
