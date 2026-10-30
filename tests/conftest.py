import os

# Never let a test start the dashboard's retention jobs: they would run
# against the REAL server/dashboard.db (server/app.py lifespan). Must be set
# before server.config is imported.
os.environ["DASHBOARD_HOUSEKEEPING"] = "false"

pytest_plugins = ('pytest_asyncio',)
