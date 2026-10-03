import asyncio
from core.database import engine
import sqlalchemy as sa

async def migrate():
    async with engine.begin() as conn:
        cols = [
            ('schedule_mode', 'VARCHAR(16) DEFAULT "exact"'),
            ('upload_hour', 'INTEGER DEFAULT 14'),
            ('upload_minute', 'INTEGER DEFAULT 0'),
            ('pre_process_minutes', 'INTEGER DEFAULT 30'),
            ('next_process_at', 'VARCHAR(32)'),
        ]
        for col_name, col_def in cols:
            try:
                await conn.execute(sa.text(f'ALTER TABLE schedule_configs ADD COLUMN {col_name} {col_def}'))
                print(f'Added column: {col_name}')
            except Exception as e:
                print(f'Column {col_name} already exists or error: {e}')

asyncio.run(migrate())
print('Migration done!')
