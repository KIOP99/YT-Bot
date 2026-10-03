import sqlite3

def migrate():
    conn = sqlite3.connect("ytbot.db")
    c = conn.cursor()
    cols = [
        ("webhook_url", "VARCHAR(255)"),
        ("button_label", "VARCHAR(64) DEFAULT '▶️ PLAY VIDEO NOW'"),
        ("button_label_2", "VARCHAR(64)"),
        ("button_url_2", "VARCHAR(255)"),
        ("accent_color", "VARCHAR(16) DEFAULT '#00A2C7'"),
        ("heading_title", "VARCHAR(128) DEFAULT '▶️ WATCH VIDEO NOW'"),
        ("features_text", "TEXT"),
        ("footer_text", "TEXT"),
        ("use_components_v2", "BOOLEAN DEFAULT 1"),
        ("button_in_box", "BOOLEAN DEFAULT 1"),
    ]
    c.execute("PRAGMA table_info(discord_configs);")
    existing = [row[1] for row in c.fetchall()]
    for col_name, col_type in cols:
        if col_name not in existing:
            c.execute(f"ALTER TABLE discord_configs ADD COLUMN {col_name} {col_type};")
            print(f"Added column: {col_name}")
        else:
            print(f"Column already exists: {col_name}")
    conn.commit()
    conn.close()
    print("Migration finished!")

if __name__ == "__main__":
    migrate()
