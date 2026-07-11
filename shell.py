from dotenv import load_dotenv

load_dotenv()

from mentor_slots import get_db

conn = get_db()
cursor = conn.cursor()

print("WordComet database connected.")
print("Available variables: conn, cursor")
print("Run conn.commit() after INSERT, UPDATE, or DELETE.")