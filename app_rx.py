import os

os.environ["APP_DEPT_PROFILE"] = "DRS"

exec(  # noqa: S102
    open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py"), encoding="utf-8").read()
)
