# main.py
import os
import sqlite3
import datetime
from typing import Dict, Any
from dotenv import load_dotenv

load_dotenv()

import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from zoneinfo import ZoneInfo

# ===== 配置参数 (无变化) =====
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "localhost")
POSTGRES_PORT = int(os.getenv("POSTGRES_PORT", "5432"))
POSTGRES_USER = os.getenv("POSTGRES_USER", "postgres")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "")
POSTGRES_DB = os.getenv("POSTGRES_DB", "defaultdb")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
SQLITE_PATH = os.getenv("SQLITE_PATH", "./admin.db")
TZ = os.getenv("TZ", "Asia/Shanghai")

# ===== FastAPI 初始化 (无变化) =====
app = FastAPI(title="Admin User Management Service")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=False, allow_methods=["*"], allow_headers=["*"],
)

# ===== SQLite 初始化 (无变化) =====
os.makedirs(os.path.dirname(SQLITE_PATH) or ".", exist_ok=True)
sqlite_conn = sqlite3.connect(SQLITE_PATH, check_same_thread=False)
sqlite_conn.execute("CREATE TABLE IF NOT EXISTS admin_sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT NOT NULL, created_at TEXT NOT NULL);")
sqlite_conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);")
sqlite_conn.commit()

# ===== Settings 帮助函数 (无变化) =====
def get_setting(key: str, default: str) -> str:
    cur = sqlite_conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    return row[0] if row else default

def set_setting(key: str, value: str):
    sqlite_conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    sqlite_conn.commit()

# ===== PostgreSQL 工具函数 (无变化) =====
def postgres_conn():
    return psycopg2.connect(host=POSTGRES_HOST, port=POSTGRES_PORT, user=POSTGRES_USER, password=POSTGRES_PASSWORD, database=POSTGRES_DB, cursor_factory=RealDictCursor)

def reset_user_quota(user_id: int, new_quota: int):
    conn = postgres_conn()
    try:
        with conn.cursor() as cur:
            cur.execute('UPDATE users SET quota = %s, used_quota = 0 WHERE id = %s AND "deleted_at" IS NULL;', (new_quota, user_id))
            conn.commit()
    finally: conn.close()

# ... 其他数据库函数保持不变 ...
def get_user_by_id(user_id: int) -> Dict[str, Any]:
    conn = postgres_conn()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT id, username, display_name, "group", quota, used_quota FROM users WHERE id = %s AND "deleted_at" IS NULL LIMIT 1;', (user_id,))
            row = cur.fetchone()
            if not row: raise HTTPException(404, "User not found or has been deleted")
            return dict(row)
    finally: conn.close()

def update_user_group(user_id: int, group: str):
    conn = postgres_conn()
    try:
        with conn.cursor() as cur:
            cur.execute('UPDATE users SET "group" = %s WHERE id = %s AND "deleted_at" IS NULL;', (group, user_id))
            conn.commit()
    finally: conn.close()

def increment_user_quota(user_id: int, delta: int):
    conn = postgres_conn()
    try:
        with conn.cursor() as cur:
            cur.execute('UPDATE users SET quota = quota + %s WHERE id = %s AND "deleted_at" IS NULL;', (delta, user_id))
            conn.commit()
    finally: conn.close()

# ===== Pydantic 模型 (无变化) =====
class AdminLoginPayload(BaseModel): password: str
class UserGroupUpdatePayload(BaseModel): user_id: int; group: str
class UserQuotaUpdatePayload(BaseModel): user_id: int; delta: int
class UserQuotaResetPayload(BaseModel): user_id: int; quota: int
class DailyResetSettingsPayload(BaseModel): vip_quota: int; default_quota: int

# ===== 管理员登录验证 (无变化) =====
def require_admin_auth(request: Request):
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(403, "Admin access required")
    token = auth_header[7:]
    if not sqlite_conn.execute("SELECT 1 FROM admin_sessions WHERE token=?", (token,)).fetchone():
        raise HTTPException(403, "Admin access required")

# ===== 路由 (新增一个接口) =====
@app.get("/", response_class=HTMLResponse)
def index():
    with open("./index.html", "r", encoding="utf-8") as f: return HTMLResponse(f.read())

# ... 其他路由保持不变 ...
@app.post("/api/admin/login")
def admin_login(payload: AdminLoginPayload):
    if payload.password != ADMIN_PASSWORD: raise HTTPException(401, "Invalid admin password")
    token = os.urandom(16).hex()
    sqlite_conn.execute("INSERT INTO admin_sessions(token, created_at) VALUES (?, ?)", (token, datetime.datetime.utcnow().isoformat()))
    sqlite_conn.commit()
    return {"token": token}

@app.get("/api/admin/settings/daily_reset")
def get_daily_reset_settings(request: Request):
    require_admin_auth(request)
    vip_quota = get_setting("daily_reset_quota_vip", "1000000")
    default_quota = get_setting("daily_reset_quota_default", "50000")
    return {"vip_quota": int(vip_quota), "default_quota": int(default_quota)}

@app.post("/api/admin/settings/daily_reset")
def set_daily_reset_settings(payload: DailyResetSettingsPayload, request: Request):
    require_admin_auth(request)
    if payload.vip_quota < 0 or payload.default_quota < 0: raise HTTPException(400, "Quota cannot be negative")
    set_setting("daily_reset_quota_vip", str(payload.vip_quota))
    set_setting("daily_reset_quota_default", str(payload.default_quota))
    return {"ok": True, "message": "Settings saved successfully."}

# --- 新增：立即触发重置任务的接口 ---
@app.post("/api/admin/actions/trigger_daily_reset")
async def trigger_daily_reset_now(request: Request):
    require_admin_auth(request)
    # 直接调用现有的异步任务函数
    await daily_reset(triggered_by="manual")
    return {"ok": True, "message": "Immediate reset task has been triggered successfully."}

# ... 用户管理路由保持不变 ...
@app.get("/api/admin/user/{user_id}")
def get_user_info(user_id: int, request: Request):
    require_admin_auth(request); return get_user_by_id(user_id)
@app.post("/api/admin/user/group")
def update_user_group_api(payload: UserGroupUpdatePayload, request: Request):
    require_admin_auth(request); update_user_group(payload.user_id, payload.group); return {"ok": True}
@app.post("/api/admin/user/quota/increment")
def increment_quota_api(payload: UserQuotaUpdatePayload, request: Request):
    require_admin_auth(request); increment_user_quota(payload.user_id, payload.delta); return {"ok": True}
@app.post("/api/admin/user/quota/reset")
def reset_quota_api(payload: UserQuotaResetPayload, request: Request):
    require_admin_auth(request); reset_user_quota(payload.user_id, payload.quota); return {"ok": True}

# ===== 定时任务 (逻辑更新，增加触发源日志) =====
async def daily_reset(triggered_by: str = "scheduled"):
    print(f"[{datetime.datetime.now()}] 开始执行配额重置任务 (触发源: {triggered_by})...")
    try:
        vip_quota = int(get_setting("daily_reset_quota_vip", "1000000"))
        default_quota = int(get_setting("daily_reset_quota_default", "50000"))
        
        conn = postgres_conn()
        with conn.cursor() as cur:
            cur.execute('SELECT id, "group" FROM users WHERE "group" IN (%s, %s) AND "deleted_at" IS NULL;', ('vip', 'default'))
            users_to_reset = cur.fetchall()
            
            print(f"发现 {len(users_to_reset)} 个 VIP 或 Default 用户需要重置配额。")
            
            for user_row in users_to_reset:
                user_id, user_group = user_row['id'], user_row['group']
                quota_to_set = vip_quota if user_group == 'vip' else default_quota
                reset_user_quota(user_id, quota_to_set)
                print(f"  - 已重置 {user_group.upper()} 用户 {user_id} 的配额为 {quota_to_set}")
        
        conn.close()
        print(f"[{datetime.datetime.now()}] 配额重置任务完成。")
    except Exception as e:
        print(f"[{datetime.datetime.now()}] 配额重置任务执行失败: {e}")

scheduler = AsyncIOScheduler(timezone=ZoneInfo(TZ))
scheduler.add_job(daily_reset, CronTrigger(hour=0, minute=0))
scheduler.start()

# ===== 入口 (无变化) =====
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8000")), reload=False)
