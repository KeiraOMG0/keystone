from database.connection import Database


class BaseRepository:
    def __init__(self, db: Database):
        self._db = db
