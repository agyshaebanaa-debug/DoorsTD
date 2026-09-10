from __future__ import annotations

"""TD Bot-as-a-Service: standalone aiogram 3.x application.

The root bot manages child Tower Defense bots. Each child has an encrypted token,
its own SQLite database and its own long-polling asyncio task. Doors-specific lore
and items are intentionally absent; units, mobs and maps are generic/configurable.
"""

import asyncio
import csv
import io
import json
import logging
import os
import secrets
import string
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from enum import StrEnum
from typing import Awaitable, Callable

import aiosqlite
from cryptography.fernet import Fernet
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramUnauthorizedError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Message, PreCheckoutQuery

UTC = timezone.utc
log = logging.getLogger("doortstdbot")


@dataclass(frozen=True, slots=True)
class Settings:
    root_bot_token: str
    root_admin_id: int
    encryption_key: str
    data_dir: Path
    log_level: str = "INFO"

    @classmethod
    def load(cls) -> "Settings":
        token = os.getenv("ROOT_BOT_TOKEN", "").strip()
        admin = os.getenv("ROOT_ADMIN_ID", "").strip()
        key = os.getenv("TOKEN_ENCRYPTION_KEY", "").strip()
        if not token or not admin.isdigit() or not key:
            raise RuntimeError("ROOT_BOT_TOKEN, numeric ROOT_ADMIN_ID and TOKEN_ENCRYPTION_KEY are required")
        data = Path(os.getenv("DATA_DIR", "./data")).resolve()
        data.mkdir(parents=True, exist_ok=True)
        return cls(token, int(admin), key, data, os.getenv("LOG_LEVEL", "INFO").upper())


class PlanCode(StrEnum):
    FREE = "free"
    COINS = "coins"
    STARS_LITE = "stars_lite"
    STUDIO = "studio"


@dataclass(frozen=True, slots=True)
class Plan:
    code: PlanCode
    name: str
    price: str
    players: int | None
    battles: int | None
    units: int | None
    maps: int | None
    lobby: int
    csv: bool
    banners: bool
    unlimited_broadcast: bool


PLANS = {
    PlanCode.FREE: Plan(PlanCode.FREE, "🟢 Free", "0", 5_000, 25, 40, 15, 4, False, False, False),
    PlanCode.COINS: Plan(PlanCode.COINS, "🟡 Coins", "4 000 монет / 30 дней", 15_000, 60, 100, 35, 4, True, False, False),
    PlanCode.STARS_LITE: Plan(PlanCode.STARS_LITE, "⭐ Stars Lite", "50 Stars / 30 дней", 40_000, 120, 250, 70, 4, True, True, False),
    PlanCode.STUDIO: Plan(PlanCode.STUDIO, "👑 Studio Ultra", "150 Stars или 20 000 монет / 30 дней", None, None, None, 6, True, True, True),
}


class TokenVault:
    def __init__(self, key: str) -> None:
        self.cipher = Fernet(key.encode())

    def encrypt(self, token: str) -> str:
        return self.cipher.encrypt(token.encode()).decode()

    def decrypt(self, value: str) -> str:
        return self.cipher.decrypt(value.encode()).decode()


def now() -> float:
    return datetime.now(UTC).timestamp()


def expiry(ts: float | None) -> str:
    return "∞" if ts is None else datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M UTC")


PLATFORM_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS child_bots(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 telegram_bot_id INTEGER UNIQUE NOT NULL,
 owner_id INTEGER NOT NULL,
 username TEXT NOT NULL,
 token_enc TEXT NOT NULL,
 plan TEXT NOT NULL DEFAULT 'free',
 expires_at REAL,
 enabled INTEGER NOT NULL DEFAULT 1,
 created_at REAL NOT NULL,
 updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_child_owner ON child_bots(owner_id);
CREATE TABLE IF NOT EXISTS wallets(user_id INTEGER PRIMARY KEY, coins INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id INTEGER NOT NULL, action TEXT NOT NULL, bot_id INTEGER, payload TEXT, created_at REAL NOT NULL);
"""

CHILD_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS players(user_id INTEGER PRIMARY KEY, username TEXT NOT NULL DEFAULT '', first_name TEXT NOT NULL DEFAULT '', level INTEGER NOT NULL DEFAULT 1, exp INTEGER NOT NULL DEFAULT 0, base_hp INTEGER NOT NULL DEFAULT 100, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS balances(user_id INTEGER PRIMARY KEY, coins INTEGER NOT NULL DEFAULT 500, FOREIGN KEY(user_id) REFERENCES players(user_id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS units(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, cost INTEGER NOT NULL, damage REAL NOT NULL, attack_speed REAL NOT NULL, attack_range REAL NOT NULL, attack_type TEXT NOT NULL, target_type TEXT NOT NULL, splash REAL NOT NULL DEFAULT 0, slow REAL NOT NULL DEFAULT 0, slow_duration REAL NOT NULL DEFAULT 0, stun_duration REAL NOT NULL DEFAULT 0, burn_damage REAL NOT NULL DEFAULT 0, burn_duration REAL NOT NULL DEFAULT 0, income INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS mobs(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, hp REAL NOT NULL, speed REAL NOT NULL, reward INTEGER NOT NULL, base_damage INTEGER NOT NULL DEFAULT 1, traits TEXT NOT NULL DEFAULT '[]', enabled INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS maps(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, max_waves INTEGER NOT NULL, start_hp INTEGER NOT NULL DEFAULT 100, waves TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS inventories(user_id INTEGER NOT NULL, unit_id INTEGER NOT NULL, quantity INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(user_id,unit_id));
CREATE TABLE IF NOT EXISTS decks(user_id INTEGER PRIMARY KEY, units TEXT NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS banners(id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS lobbies(code TEXT PRIMARY KEY, map_id INTEGER NOT NULL, owner_id INTEGER NOT NULL, max_players INTEGER NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS lobby_members(code TEXT NOT NULL, user_id INTEGER NOT NULL, joined_at REAL NOT NULL, PRIMARY KEY(code,user_id));
"""


class DB:
    def __init__(self, path: Path, schema: str) -> None:
        self.path = path
        self.schema = schema
        self.conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        if self.conn is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(self.schema)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()
            self.conn = None

    def c(self) -> aiosqlite.Connection:
        if self.conn is None:
            raise RuntimeError("database is closed")
        return self.conn

    async def exec(self, sql: str, params: tuple = ()) -> None:
        await self.c().execute(sql, params)
        await self.c().commit()

    async def one(self, sql: str, params: tuple = ()) -> aiosqlite.Row | None:
        cur = await self.c().execute(sql, params)
        return await cur.fetchone()

    async def all(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        cur = await self.c().execute(sql, params)
        return await cur.fetchall()

    async def scalar(self, sql: str, params: tuple = ()):
        row = await self.one(sql, params)
        return row[0] if row else None


class ChildDB(DB):
    async def seed(self) -> None:
        if int(await self.scalar("SELECT COUNT(*) FROM units") or 0) == 0:
            units = [
                ("Scout",100,18,1.2,30,"Одиночный","Универсал",0,0,0,0,0,0,0),
                ("Cannon",250,55,2.2,24,"Сплеш","Наземный",8,0,0,0,0,0,0),
                ("Frost",300,22,1.5,28,"Замедление","Универсал",0,.35,2.5,0,0,0,0),
                ("Stunner",400,36,2.8,26,"Оглушение","Универсал",0,0,0,1.3,0,0,0),
                ("Burner",350,30,1.7,27,"Горение","Универсал",0,0,0,0,10,3,0),
                ("Generator",200,6,4.0,18,"Ферма","Наземный",0,0,0,0,0,0,20),
            ]
            for u in units:
                await self.exec("INSERT INTO units(name,cost,damage,attack_speed,attack_range,attack_type,target_type,splash,slow,slow_duration,stun_duration,burn_damage,burn_duration,income,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (*u, now()))
        if int(await self.scalar("SELECT COUNT(*) FROM mobs") or 0) == 0:
            for m in [("Grunt",80,8,12,1,["Обычный"]),("Runner",45,14,15,1,["Обычный"]),("Air Scout",70,9,18,2,["Летающий"]),("Cloaked",120,7,24,2,["Камуфляж"]),("Titan",800,3.5,100,8,["Обычный"])]:
                await self.exec("INSERT INTO mobs(name,hp,speed,reward,base_damage,traits,created_at) VALUES(?,?,?,?,?,?,?)", (m[0],m[1],m[2],m[3],m[4],json.dumps(m[5],ensure_ascii=False),now()))
        if int(await self.scalar("SELECT COUNT(*) FROM maps") or 0) == 0:
            ids = [int(r[0]) for r in await self.all("SELECT id FROM mobs ORDER BY id")]
            waves=[]
            for w in range(1,11):
                waves.append({"wave":w,"mob":ids[(w-1)%len(ids)],"count":4+w})
                if w >= 4:
                    waves.append({"wave":w,"mob":ids[2],"count":1+w//4})
                if w == 10:
                    waves.append({"wave":w,"mob":ids[-1],"count":1})
            await self.exec("INSERT INTO maps(name,max_waves,start_hp,waves,created_at) VALUES(?,?,?,?,?)", ("Training Grounds",10,100,json.dumps(waves),now()))

    async def player(self, uid: int, username: str = "", first: str = "") -> aiosqlite.Row:
        row = await self.one("SELECT * FROM players WHERE user_id=?", (uid,))
        if row:
            await self.exec("UPDATE players SET username=?,first_name=?,updated_at=? WHERE user_id=?", (username,first,now(),uid))
            return row
        await self.exec("INSERT INTO players(user_id,username,first_name,created_at,updated_at) VALUES(?,?,?,?,?)", (uid,username,first,now(),now()))
        await self.exec("INSERT INTO balances(user_id,coins) VALUES(?,500)", (uid,))
        unit = await self.scalar("SELECT id FROM units ORDER BY id LIMIT 1")
        if unit:
            await self.exec("INSERT INTO inventories(user_id,unit_id,quantity) VALUES(?,?,1)", (uid,int(unit)))
            await self.exec("INSERT INTO decks(user_id,units) VALUES(?,?)", (uid,json.dumps([int(unit)])))
        return await self.one("SELECT * FROM players WHERE user_id=?", (uid,))


@dataclass(slots=True)
class Mob:
    uid: int
    name: str
    hp: float
    speed: float
    reward: int
    base_damage: int
    traits: tuple[str,...]
    pos: float = 100.0
    slow: float = 0.0
    slow_until: float = 0.0
    stun_until: float = 0.0
    burn_damage: float = 0.0
    burn_until: float = 0.0


@dataclass(slots=True)
class Tower:
    owner: int
    unit_id: int
    name: str
    cost: int
    damage: float
    attack_speed: float
    attack_range: float
    target_type: str
    splash: float
    slow: float
    slow_duration: float
    stun_duration: float
    burn_damage: float
    burn_duration: float
    income: int
    pos: float
    cooldown: float = 0.0


class Battle:
    def __init__(self, bid: str, map_name: str, max_waves: int, base_hp: int, waves: dict[int,list[dict]], members: dict[int,int], notify: Callable[[str],Awaitable[None]]) -> None:
        self.bid,self.map_name,self.max_waves,self.base_hp = bid,map_name,max_waves,base_hp
        self.max_hp=base_hp; self.waves=waves; self.members=members; self.notify=notify
        self.wave=0; self.mobs:dict[int,Mob]={}; self.towers:list[Tower]=[]; self.next_uid=1; self.stop_event=asyncio.Event(); self.lock=asyncio.Lock(); self.phase="prepare"

    async def start(self, unit_rows: dict[int,dict]) -> None:
        self.phase="wave"
        await self.notify(f"⚔️ Бой <b>#{self.bid}</b> начат на <b>{self.map_name}</b>.")
        await self.spawn()
        last=time.monotonic(); income_tick=0.0
        while not self.stop_event.is_set():
            await asyncio.sleep(.5)
            nowm=time.monotonic(); dt=min(2.0,nowm-last); last=nowm; income_tick+=dt
            async with self.lock:
                for m in list(self.mobs.values()):
                    if m.hp <= 0: continue
                    if m.burn_until > nowm: m.hp -= m.burn_damage*dt
                    if m.hp <= 0: continue
                    if nowm < m.stun_until: continue
                    mul=max(.1,1-m.slow) if m.slow_until>nowm else 1.0
                    m.pos -= m.speed*mul*dt
                    if m.pos <= 0:
                        self.base_hp -= m.base_damage; m.hp=0
                if income_tick>=5:
                    income_tick=0
                    for t in self.towers:
                        if t.income:
                            self.members[t.owner]+=t.income
                for t in self.towers:
                    if t.cooldown>nowm: continue
                    candidates=[m for m in self.mobs.values() if m.hp>0 and self.valid(t,m)]
                    if not candidates: continue
                    target=max(candidates,key=lambda x:x.pos)
                    hits=[target]
                    if t.splash>0: hits=[m for m in candidates if abs(m.pos-target.pos)<=t.splash]
                    for m in hits:
                        dmg=t.damage*(2 if secrets.randbelow(100)<8 else 1); m.hp-=dmg
                        if t.slow and t.slow_duration: m.slow=t.slow; m.slow_until=nowm+t.slow_duration
                        if t.stun_duration: m.stun_until=nowm+t.stun_duration
                        if t.burn_damage and t.burn_duration: m.burn_damage=t.burn_damage; m.burn_until=nowm+t.burn_duration
                        self.members[t.owner]+=int(dmg*.5)
                    t.cooldown=nowm+max(.05,t.attack_speed)
                dead=[m for m in self.mobs.values() if m.hp<=0]
                for m in dead:
                    winner=next(iter(self.members)) if self.members else None
                    if winner is not None: self.members[winner]+=m.reward
                    self.mobs.pop(m.uid,None)
                if self.base_hp<=0:
                    self.phase="defeat"; self.stop_event.set(); await self.notify(f"💥 Бой <b>#{self.bid}</b>: поражение."); continue
                if not self.mobs:
                    if self.wave>=self.max_waves:
                        self.phase="victory"; self.stop_event.set(); reward=50+self.max_waves*10
                        for uid in self.members: self.members[uid]+=reward
                        await self.notify(f"🏆 Бой <b>#{self.bid}</b>: победа. +{reward} монет каждому.")
                    else:
                        await self.spawn()

    def valid(self,t:Tower,m:Mob)->bool:
        if abs(t.pos-m.pos)>t.attack_range: return False
        traits=set(m.traits); kinds={x.strip() for x in t.target_type.split(",")}
        if "Летающий" in traits and not ({"Анти-Воздух","Универсал"}&kinds): return False
        if "Камуфляж" in traits and not ({"Детектор","Универсал"}&kinds): return False
        return True

    async def spawn(self)->None:
        self.wave+=1; entries=self.waves.get(self.wave,[])
        for e in entries:
            for _ in range(max(1,int(e["count"]))):
                self.mobs[self.next_uid]=Mob(self.next_uid,e["name"],e["hp"],e["speed"],e["reward"],e["base_damage"],tuple(e["traits"])); self.next_uid+=1
        await self.notify(f"🌊 Волна <b>{self.wave}/{self.max_waves}</b>: целей {len(self.mobs)}.")

    async def place(self,uid:int,row:dict,pos:float)->tuple[bool,str]:
        async with self.lock:
            if self.phase!="wave": return False,"Бой не принимает установку."
            if uid not in self.members: return False,"Игрок не участвует в бою."
            if self.members[uid]<int(row["cost"]): return False,"Недостаточно монет."
            if not 0<=pos<=100: return False,"Позиция 0..100."
            self.members[uid]-=int(row["cost"])
            self.towers.append(Tower(uid,int(row["id"]),row["name"],int(row["cost"]),float(row["damage"]),float(row["attack_speed"]),float(row["attack_range"]),row["target_type"],float(row["splash"]),float(row["slow"]),float(row["slow_duration"]),float(row["stun_duration"]),float(row["burn_damage"]),float(row["burn_duration"]),int(row["income"]),pos))
            return True,"Юнит установлен."


@dataclass(slots=True)
class Runtime:
    bot_id:int; owner_id:int; username:str; token:str; plan:PlanCode; expires_at:float|None; bot:Bot; db:ChildDB
    poll_task:asyncio.Task|None=None; battles:dict[str,Battle]=field(default_factory=dict); temp_cache:dict[str,object]=field(default_factory=dict)
    @property
    def running(self)->bool:return self.poll_task is not None and not self.poll_task.done()


class Manager:
    def __init__(self,dispatcher:Dispatcher,settings:Settings,vault:TokenVault,root_id:int)->None:
        self.dp=dispatcher; self.settings=settings; self.vault=vault; self.root_id=root_id
        self.platform=DB(settings.data_dir/"platform.db",PLATFORM_SCHEMA); self.runtimes:dict[int,Runtime]={}; self.lock=asyncio.Lock(); self.expiry_task:asyncio.Task|None=None

    async def open(self)->None:
        await self.platform.open(); await self.sync_expiry()
        for r in await self.platform.all("SELECT telegram_bot_id FROM child_bots WHERE enabled=1"):
            try: await self.start(int(r[0]))
            except Exception: log.exception("failed to restore child %s",r[0])
        self.expiry_task=asyncio.create_task(self.expiry_loop(),name="expiry-loop")

    async def close(self)->None:
        if self.expiry_task: self.expiry_task.cancel(); await asyncio.gather(self.expiry_task,return_exceptions=True)
        for bid in list(self.runtimes): await self.stop(bid)
        await self.platform.close()

    async def expiry_loop(self)->None:
        while True:
            await asyncio.sleep(60); await self.sync_expiry()

    async def sync_expiry(self)->None:
        rows=await self.platform.all("SELECT telegram_bot_id FROM child_bots WHERE plan!='free' AND expires_at IS NOT NULL AND expires_at<=?",(now(),))
        for r in rows:
            bid=int(r[0]); await self.platform.exec("UPDATE child_bots SET plan='free',expires_at=NULL,updated_at=? WHERE telegram_bot_id=?",(now(),bid))
            rt=self.runtimes.get(bid)
            if rt: rt.plan=PlanCode.FREE; rt.expires_at=None; rt.temp_cache.clear()

    async def child_db(self,bid:int)->ChildDB:
        db=ChildDB(self.settings.data_dir/f"child_{bid}.db",CHILD_SCHEMA); await db.open(); await db.seed(); return db

    async def register(self,owner:int,raw_token:str)->Runtime:
        probe=Bot(raw_token,default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        try: me=await probe.get_me()
        finally: await probe.session.close()
        if await self.platform.one("SELECT 1 FROM child_bots WHERE telegram_bot_id=?",(int(me.id),)): raise ValueError("Этот бот уже подключён.")
        await self.platform.exec("INSERT INTO child_bots(telegram_bot_id,owner_id,username,token_enc,plan,expires_at,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",(int(me.id),owner,me.username or str(me.id),self.vault.encrypt(raw_token),'free',None,1,now(),now()))
        await self.platform.exec("INSERT INTO wallets(user_id,coins,updated_at) VALUES(?,?,?) ON CONFLICT(user_id) DO NOTHING",(owner,0,now()))
        return await self.build(int(me.id))

    async def build(self,bid:int)->Runtime:
        if bid in self.runtimes:return self.runtimes[bid]
        row=await self.platform.one("SELECT * FROM child_bots WHERE telegram_bot_id=?",(bid,))
        if not row: raise ValueError("Дочерний бот не найден.")
        db=await self.child_db(bid); rt=Runtime(bid,int(row["owner_id"]),row["username"],self.vault.decrypt(row["token_enc"]),PlanCode(row["plan"]),row["expires_at"],Bot(self.vault.decrypt(row["token_enc"]),default=DefaultBotProperties(parse_mode=ParseMode.HTML)),db); self.runtimes[bid]=rt; return rt

    async def poll(self,rt:Runtime)->None:
        offset=0; allowed=self.dp.resolve_used_update_types(); await rt.bot.delete_webhook(drop_pending_updates=False)
        while True:
            try:
                updates=await rt.bot.get_updates(offset=offset,timeout=25,allowed_updates=allowed)
                for update in updates:
                    offset=update.update_id+1; await self.dp.feed_update(rt.bot,update)
            except asyncio.CancelledError: raise
            except TelegramUnauthorizedError:
                await self.platform.exec("UPDATE child_bots SET enabled=0,updated_at=? WHERE telegram_bot_id=?",(now(),rt.bot_id)); break
            except TelegramAPIError:
                log.exception("child polling error %s",rt.bot_id); await asyncio.sleep(2)
            except Exception:
                log.exception("unexpected child polling error %s",rt.bot_id); await asyncio.sleep(2)

    async def start(self,bid:int)->Runtime:
        async with self.lock:
            rt=await self.build(bid)
            if rt.running:return rt
            await self.platform.exec("UPDATE child_bots SET enabled=1,updated_at=? WHERE telegram_bot_id=?",(now(),bid)); rt.temp_cache.clear(); rt.poll_task=asyncio.create_task(self.poll(rt),name=f"child-poll-{bid}"); return rt

    async def stop(self,bid:int)->None:
        async with self.lock:
            rt=self.runtimes.get(bid); await self.platform.exec("UPDATE child_bots SET enabled=0,updated_at=? WHERE telegram_bot_id=?",(now(),bid))
            if not rt:return
            task=rt.poll_task; rt.poll_task=None
            if task and not task.done(): task.cancel(); await asyncio.gather(task,return_exceptions=True)
            for b in list(rt.battles.values()): b.stop_event.set()
            rt.battles.clear(); rt.temp_cache.clear(); await rt.bot.session.close(); await rt.db.close(); self.runtimes.pop(bid,None)

    async def restart(self,bid:int)->Runtime:
        await self.stop(bid); return await self.start(bid)

    async def set_plan(self,bid:int,plan:PlanCode,days:int,actor:int)->None:
        expires=None if plan==PlanCode.FREE else now()+days*86400; await self.platform.exec("UPDATE child_bots SET plan=?,expires_at=?,updated_at=? WHERE telegram_bot_id=?",(plan.value,expires,now(),bid)); rt=self.runtimes.get(bid)
        if rt: rt.plan=plan; rt.expires_at=expires; rt.temp_cache.clear()
        await self.platform.exec("INSERT INTO events(actor_id,action,bot_id,payload,created_at) VALUES(?,?,?,?,?)",(actor,"plan",bid,json.dumps({"plan":plan.value,"days":days}),now()))

    async def add_days(self,bid:int,days:int,actor:int)->None:
        row=await self.platform.one("SELECT plan,expires_at FROM child_bots WHERE telegram_bot_id=?",(bid,))
        if not row: raise ValueError("Бот не найден")
        plan=PlanCode(row["plan"])
        if plan==PlanCode.FREE: raise ValueError("Для Free сначала установите платный тариф через /set_plan")
        exp=float(row["expires_at"] or now())+days*86400; await self.platform.exec("UPDATE child_bots SET expires_at=?,updated_at=? WHERE telegram_bot_id=?",(exp,now(),bid)); rt=self.runtimes.get(bid)
        if rt: rt.expires_at=exp

    async def limit(self,rt:Runtime,kind:str,current:int)->None:
        limit=getattr(PLANS[rt.plan],kind)
        if limit is not None and current>=limit: raise ValueError(f"Лимит тарифа {PLANS[rt.plan].name}: {limit}")


class RoleMiddleware(BaseMiddleware):
    def __init__(self,manager:Manager,root_id:int)->None:self.manager,self.root_id=manager,root_id
    async def __call__(self,handler,event,data):
        bot:Bot=data["bot"]; data["is_root"]=bot.id==self.root_id; data["is_child"]=bot.id!=self.root_id and bot.id in self.manager.runtimes
        if data["is_child"]: data["runtime"]=self.manager.runtimes[bot.id]
        return await handler(event,data)


class TokenState(StatesGroup): waiting=State()
class TextState(StatesGroup): waiting=State()
class ImportState(StatesGroup): waiting=State()

def root_only(fn):
    async def wrap(event,data): return await fn(event,data) if data.get("is_root") else None
    return wrap

def child_only(fn):
    async def wrap(event,data): return await fn(event,data) if data.get("is_child") else None
    return wrap


def root_keyboard()->InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🤖 Мои боты",callback_data="root:mybots")],[InlineKeyboardButton(text="➕ Добавить",callback_data="root:add")],[InlineKeyboardButton(text="💳 Тарифы",callback_data="root:plans")],[InlineKeyboardButton(text="📡 Сеть",callback_data="root:network")]])


def child_admin_keyboard()->InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="▶️ Start",callback_data="child:start"),InlineKeyboardButton(text="⏸ Stop",callback_data="child:stop")],[InlineKeyboardButton(text="🔄 Restart",callback_data="child:restart")],[InlineKeyboardButton(text="📊 Status",callback_data="child:status")]])


def plan_keyboard()->InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🟡 Coins",callback_data="buy:coins")],[InlineKeyboardButton(text="⭐ Stars Lite",callback_data="buy:stars_lite")],[InlineKeyboardButton(text="👑 Studio 150 Stars",callback_data="buy:studio_stars")],[InlineKeyboardButton(text="👑 Studio 20k coins",callback_data="buy:studio_coins")]])


async def build_dispatcher(manager:Manager,root_id:int)->Dispatcher:
    dp=Dispatcher(); role=RoleMiddleware(manager,root_id); dp.message.middleware(role); dp.callback_query.middleware(role); dp.pre_checkout_query.middleware(role)
    root=Router(name="root"); child=Router(name="child")

    @root.message(CommandStart())
    @root_only
    async def start_root(message:Message): await message.answer("<b>TD Bot-as-a-Service</b>\n\nПодключай независимые Tower Defense боты без перезапуска root-процесса.\n\n/addbot — подключить токен\n/mybots — мои боты\n/plans — тарифы\n/wallet — баланс root-кошелька\n/admin — панель администратора",reply_markup=root_keyboard())

    @root.message(Command("addbot"))
    @root_only
    async def addbot(message:Message,state:FSMContext): await state.set_state(TokenState.waiting); await message.answer("Пришли токен из @BotFather. После проверки сообщение с токеном удаляется.")

    @root.message(TokenState.waiting)
    @root_only
    async def addbot_token(message:Message,state:FSMContext):
        token=(message.text or "").strip()
        try:
            rt=await manager.register(message.from_user.id,token); await manager.start(rt.bot_id); await state.clear();
            try: await message.delete()
            except TelegramAPIError: pass
            await message.answer(f"✅ Подключён @{rt.username}. BOT_ID=<code>{rt.bot_id}</code>")
        except Exception as exc: await message.answer(f"❌ Не удалось подключить: {str(exc)[:500]}")

    @root.message(Command("mybots"))
    @root_only
    async def mybots(message:Message):
        rows=await manager.platform.all("SELECT * FROM child_bots WHERE owner_id=? ORDER BY id DESC",(message.from_user.id,))
        if not rows: await message.answer("Ботов пока нет.",reply_markup=root_keyboard()); return
        text="<b>Мои дочерние боты</b>\n"+"\n".join(f"{'🟢' if manager.runtimes.get(int(r['telegram_bot_id'])) and manager.runtimes[int(r['telegram_bot_id'])].running else '⛔'} @{r['username']} · {PLANS[PlanCode(r['plan'])].name} · до {expiry(r['expires_at'])}\n<code>/buy {r['telegram_bot_id']} coins|stars_lite|studio_stars|studio_coins</code>" for r in rows)
        await message.answer(text,reply_markup=root_keyboard())

    @root.message(Command("wallet"))
    @root_only
    async def wallet(message:Message):
        row=await manager.platform.one("SELECT coins FROM wallets WHERE user_id=?",(message.from_user.id,)); balance=int(row[0]) if row else 0; await message.answer(f"💰 Root-кошелёк: <b>{balance}</b> монет")

    @root.message(Command("plans"))
    @root_only
    async def plans(message:Message):
        text="\n".join(f"{p.name}: {p.price}; players={p.players or '∞'}, battles={p.battles or '∞'}, units={p.units or '∞'}, maps={p.maps or '∞'}, lobby={p.lobby}" for p in PLANS.values()); await message.answer(text+"\n\nДля покупки укажи BOT_ID командой /buy BOT_ID coins|stars_lite|studio_stars|studio_coins",reply_markup=plan_keyboard())

    @root.message(Command("buy"))
    @root_only
    async def buy(message:Message):
        parts=(message.text or "").split();
        if len(parts)!=3 or not parts[1].isdigit() or parts[2] not in {"coins","stars_lite","studio_stars","studio_coins"}: await message.answer("Использование: /buy BOT_ID coins|stars_lite|studio_stars|studio_coins"); return
        bid,mode=int(parts[1]),parts[2]; row=await manager.platform.one("SELECT * FROM child_bots WHERE telegram_bot_id=? AND owner_id=?",(bid,message.from_user.id))
        if not row: await message.answer("⛔ Бот не найден или не твой."); return
        if mode in {"coins","studio_coins"}:
            price=4000 if mode=="coins" else 20000; plan=PlanCode.COINS if mode=="coins" else PlanCode.STUDIO
            ok=await manager.platform.one("SELECT coins FROM wallets WHERE user_id=?",(message.from_user.id)); balance=int(ok[0]) if ok else 0
            if balance<price: await message.answer(f"Недостаточно монет. Баланс: {balance}"); return
            await manager.platform.exec("UPDATE wallets SET coins=coins-?,updated_at=? WHERE user_id=?",(price,now(),message.from_user.id)); await manager.set_plan(bid,plan,30,message.from_user.id); await message.answer(f"✅ {PLANS[plan].name} активирован на 30 дней.")
        else:
            stars=50 if mode=="stars_lite" else 150; await message.bot.send_invoice(chat_id=message.from_user.id,title="TD Platform",description=f"{PLANS[PlanCode.STARS_LITE if stars==50 else PlanCode.STUDIO].name} на 30 дней",payload=f"sub:{bid}:{mode}:{message.from_user.id}",currency="XTR",prices=[LabeledPrice(label="Telegram Stars",amount=stars)],provider_token="")

    @root.pre_checkout_query()
    @root_only
    async def checkout(q:PreCheckoutQuery):
        try:
            _,bid,mode,uid=q.invoice_payload.split(":"); row=await manager.platform.one("SELECT owner_id FROM child_bots WHERE telegram_bot_id=?",(int(bid),)); ok=row is not None and int(uid)==q.from_user.id and mode in {"stars_lite","studio_stars"}; await q.answer(ok=ok,error_message=None if ok else "Платёж не прошёл проверку")
        except Exception: await q.answer(ok=False,error_message="Некорректный платёж")

    @root.message(F.successful_payment)
    @root_only
    async def paid(message:Message):
        _,bid,mode,uid=message.successful_payment.invoice_payload.split(":");
        if int(uid)!=message.from_user.id:return
        plan=PlanCode.STARS_LITE if mode=="stars_lite" else PlanCode.STUDIO; await manager.set_plan(int(bid),plan,30,message.from_user.id); await message.answer(f"✅ {PLANS[plan].name} активирован на 30 дней.")

    @root.message(Command("set_plan"))
    @root_only
    async def set_plan(message:Message):
        if message.from_user.id!=manager.settings.root_admin_id: await message.answer("⛔ Нет доступа."); return
        p=(message.text or "").split();
        if len(p)!=4 or not p[1].isdigit() or p[2] not in {x.value for x in PlanCode} or not p[3].isdigit(): await message.answer("/set_plan BOT_ID free|coins|stars_lite|studio DAYS"); return
        await manager.set_plan(int(p[1]),PlanCode(p[2]),int(p[3]),message.from_user.id); await message.answer("✅ Тариф установлен.")

    @root.message(Command("add_days"))
    @root_only
    async def add_days(message:Message):
        if message.from_user.id!=manager.settings.root_admin_id: await message.answer("⛔ Нет доступа."); return
        p=(message.text or "").split();
        if len(p)!=3 or not p[1].isdigit() or not p[2].isdigit(): await message.answer("/add_days BOT_ID DAYS"); return
        try: await manager.add_days(int(p[1]),int(p[2]),message.from_user.id); await message.answer("✅ Дни добавлены.")
        except ValueError as exc: await message.answer(f"❌ {exc}")

    @root.message(Command("child_start"))
    @root.message(Command("child_stop"))
    @root.message(Command("child_restart"))
    @root_only
    async def lifecycle(message:Message):
        if message.from_user.id not in {manager.settings.root_admin_id}: await message.answer("⛔ Только root-admin."); return
        p=(message.text or "").split();
        if len(p)!=2 or not p[1].isdigit(): await message.answer(f"Использование: /{(message.text or '').split()[0].lstrip('/')} BOT_ID"); return
        bid=int(p[1]); row=await manager.platform.one("SELECT username FROM child_bots WHERE telegram_bot_id=?",(bid,));
        if not row: await message.answer("Бот не найден."); return
        cmd=(message.text or '').split()[0]
        if cmd=="/child_start":await manager.start(bid)
        elif cmd=="/child_stop":await manager.stop(bid)
        else:await manager.restart(bid)
        await message.answer(f"✅ {cmd} выполнен для @{row['username']}")

    @root.message(Command("kill"))
    @root_only
    async def kill(message:Message):
        if message.from_user.id!=manager.settings.root_admin_id: await message.answer("⛔ Только root-admin."); return
        p=(message.text or '').split();
        if len(p)==2 and p[1].isdigit(): await manager.stop(int(p[1])); await message.answer("🛑 Kill switch выполнен.")

    @root.message(Command("delete_bot"))
    @root_only
    async def delete_bot(message:Message):
        if message.from_user.id!=manager.settings.root_admin_id: await message.answer("⛔ Только root-admin."); return
        p=(message.text or '').split();
        if len(p)!=2 or not p[1].isdigit(): await message.answer("/delete_bot BOT_ID"); return
        bid=int(p[1]); await manager.stop(bid); await manager.platform.exec("DELETE FROM child_bots WHERE telegram_bot_id=?",(bid,)); (manager.settings.data_dir/f"child_{bid}.db").unlink(missing_ok=True); await message.answer("🗑 Бот удалён и его изолированная БД удалена.")

    @root.message(Command("grant_coins"))
    @root_only
    async def grant_coins(message:Message):
        if message.from_user.id!=manager.settings.root_admin_id: await message.answer("⛔ Только root-admin."); return
        p=(message.text or '').split();
        if len(p)!=3 or not p[1].isdigit() or not p[2].lstrip('-').isdigit(): await message.answer("/grant_coins USER_ID AMOUNT"); return
        uid,amount=int(p[1]),int(p[2]); await manager.platform.exec("INSERT INTO wallets(user_id,coins,updated_at) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET coins=coins+excluded.coins,updated_at=excluded.updated_at",(uid,amount,now())); await message.answer("✅ Баланс изменён.")

    @root.message(Command("admin"))
    @root_only
    async def admin(message:Message):
        if message.from_user.id!=manager.settings.root_admin_id: await message.answer("⛔ Нет доступа."); return
        await message.answer("<b>ROOT ADMIN</b>\n/network — мониторинг сети\n/broadcast — системная рассылка\n/set_plan — ручная выдача тарифа\n/add_days — продление\n/grant_coins — баланс создателя",reply_markup=root_keyboard())

    @root.message(Command("network"))
    @root_only
    async def network(message:Message):
        if message.from_user.id!=manager.settings.root_admin_id: await message.answer("⛔ Нет доступа."); return
        rows=await manager.platform.all("SELECT * FROM child_bots ORDER BY id DESC"); parts=["<b>🤖 Сеть дочерних ботов</b>"]
        for r in rows:
            bid=int(r['telegram_bot_id']); rt=manager.runtimes.get(bid); db=rt.db if rt else await manager.child_db(bid); players=int(await db.scalar("SELECT COUNT(*) FROM players") or 0); battles=len(rt.battles) if rt else 0
            if not rt: await db.close()
            parts.append(f"{'🟢' if rt and rt.running else '⛔'} @{r['username']} · owner=<code>{r['owner_id']}</code> · {PLANS[PlanCode(r['plan'])].name} · до {expiry(r['expires_at'])} · players={players} · battles={battles}")
        await message.answer("\n".join(parts))

    @root.message(Command("broadcast"))
    @root_only
    async def broadcast(message:Message,state:FSMContext):
        if message.from_user.id!=manager.settings.root_admin_id: await message.answer("⛔ Нет доступа."); return
        await state.set_state(TextState.waiting); await message.answer("Пришли текст рассылки по сети.")

    @root.message(TextState.waiting)
    @root_only
    async def broadcast_text(message:Message,state:FSMContext):
        if message.from_user.id!=manager.settings.root_admin_id:return
        await state.clear(); sent=total=0
        for rt in list(manager.runtimes.values()):
            for r in await rt.db.all("SELECT user_id FROM players"):
                total+=1
                try: await rt.bot.send_message(int(r[0]),f"📢 <b>Системное сообщение</b>\n\n{message.text or ''}"); sent+=1
                except TelegramAPIError: pass
        await message.answer(f"✅ Рассылка: {sent}/{total}")

    @root.callback_query(F.data=="root:add")
    @root_only
    async def cb_add(call:CallbackQuery,state:FSMContext): await state.set_state(TokenState.waiting); await call.message.answer("Пришли токен из @BotFather."); await call.answer()
    @root.callback_query(F.data=="root:mybots")
    @root_only
    async def cb_mybots(call:CallbackQuery): await call.message.answer("Используй /mybots"); await call.answer()
    @root.callback_query(F.data=="root:plans")
    @root_only
    async def cb_plans(call:CallbackQuery): await call.message.answer("Используй /plans"); await call.answer()
    @root.callback_query(F.data=="root:network")
    @root_only
    async def cb_network(call:CallbackQuery): await call.message.answer("Используй /network"); await call.answer()
    @root.callback_query(F.data.startswith("buy:"))
    @root_only
    async def cb_buy(call:CallbackQuery): await call.message.answer("Укажи BOT_ID: /buy BOT_ID coins|stars_lite|studio_stars|studio_coins"); await call.answer()

    @child.message(CommandStart())
    @child_only
    async def child_start(message:Message,runtime:Runtime,bot:Bot):
        await manager.limit(runtime,"players",int(await runtime.db.scalar("SELECT COUNT(*) FROM players") or 0)-1 if await runtime.db.one("SELECT user_id FROM players WHERE user_id=?",(message.from_user.id,)) else int(await runtime.db.scalar("SELECT COUNT(*) FROM players") or 0))
        await runtime.db.player(message.from_user.id,message.from_user.username or '',message.from_user.first_name or '')
        p=PLANS[runtime.plan]; banner=''
        if p.banners:
            rows=await runtime.db.all("SELECT text FROM banners WHERE enabled=1 ORDER BY id")
            if rows: banner=f"\n\n📣 {rows[int(time.time()//3600)%len(rows)][0]}"
        await message.answer(f"<b>@{runtime.username}</b>\nТариф: {p.name}\nИгроков: {await runtime.db.scalar('SELECT COUNT(*) FROM players')}/{p.players or '∞'}\nЮнитов: {await runtime.db.scalar('SELECT COUNT(*) FROM units WHERE enabled=1')}/{p.units or '∞'}\nКарт: {await runtime.db.scalar('SELECT COUNT(*) FROM maps WHERE enabled=1')}/{p.maps or '∞'}\n\n/profile /units /maps /battle /lobby /join CODE /place UNIT_ID POS /td_admin"+banner)

    @child.message(Command("profile"))
    @child_only
    async def profile(message:Message,runtime:Runtime):
        row=await runtime.db.player(message.from_user.id,message.from_user.username or '',message.from_user.first_name or ''); coins=int(await runtime.db.scalar("SELECT coins FROM balances WHERE user_id=?",(message.from_user.id,)) or 0); await message.answer(f"👤 <b>{row['first_name'] or row['username']}</b>\nУровень: {row['level']}\nEXP: {row['exp']}\nHP: {row['base_hp']}\n💰 Монеты: {coins}")

    @child.message(Command("units"))
    @child_only
    async def units(message:Message,runtime:Runtime):
        rows=await runtime.db.all("SELECT * FROM units WHERE enabled=1 ORDER BY id"); await message.answer("<b>Юниты</b>\n"+"\n".join(f"#{r['id']} {r['name']} — {r['cost']}💰 · {r['attack_type']} · {r['target_type']} · dmg {r['damage']}" for r in rows)[:3900])

    @child.message(Command("maps"))
    @child_only
    async def maps(message:Message,runtime:Runtime):
        rows=await runtime.db.all("SELECT * FROM maps WHERE enabled=1 ORDER BY id"); await message.answer("<b>Карты</b>\n"+"\n".join(f"#{r['id']} {r['name']} — {r['max_waves']} волн" for r in rows))

    @child.message(Command("battle"))
    @child_only
    async def battle(message:Message,runtime:Runtime):
        rows=await runtime.db.all("SELECT * FROM maps WHERE enabled=1 ORDER BY id"); await message.answer("Выбери карту:",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"⚔️ {r['name']}",callback_data=f"battle:{r['id']}")] for r in rows]))

    @child.callback_query(F.data.startswith("battle:"))
    @child_only
    async def battle_cb(call:CallbackQuery,runtime:Runtime):
        await manager.limit(runtime,"battles",len(runtime.battles)); mid=int(call.data.split(":")[1]); row=await runtime.db.one("SELECT * FROM maps WHERE id=? AND enabled=1",(mid,));
        if not row: await call.answer("Карта не найдена",show_alert=True); return
        mobs={int(r['id']):r for r in await runtime.db.all("SELECT * FROM mobs WHERE enabled=1")}; cfg=json.loads(row['waves']); waves={}
        for e in cfg:
            m=mobs[int(e['mob'])]; waves.setdefault(int(e['wave']),[]).append({"name":m['name'],"hp":float(m['hp']),"speed":float(m['speed']),"reward":int(m['reward']),"base_damage":int(m['base_damage']),"traits":json.loads(m['traits']),"count":int(e['count'])})
        coins=int(await runtime.db.scalar("SELECT coins FROM balances WHERE user_id=?",(call.from_user.id,)) or 0); members={call.from_user.id:coins}; bid=secrets.token_hex(3).upper()
        async def notify(text:str):
            for uid in members:
                try: await runtime.bot.send_message(uid,text)
                except TelegramAPIError: pass
        b=Battle(bid,row['name'],int(row['max_waves']),int(row['start_hp']),waves,members,notify); runtime.battles[bid]=b
        async def run():
            try:
                unit_rows={int(r['id']):dict(r) for r in await runtime.db.all("SELECT * FROM units WHERE enabled=1")}; await b.start(unit_rows)
                for uid,c in members.items(): await runtime.db.exec("UPDATE balances SET coins=? WHERE user_id=?",(c,uid))
            finally: runtime.battles.pop(bid,None)
        asyncio.create_task(run(),name=f"battle-{runtime.bot_id}-{bid}"); await call.message.answer(f"🎮 Бой <code>{bid}</code> запущен. /place UNIT_ID POSITION"); await call.answer()

    @child.message(Command("place"))
    @child_only
    async def place(message:Message,runtime:Runtime):
        p=(message.text or '').split();
        if len(p)!=3 or not p[1].isdigit(): await message.answer("/place UNIT_ID POSITION"); return
        try: pos=float(p[2])
        except ValueError: await message.answer("POSITION должна быть числом"); return
        row=await runtime.db.one("SELECT * FROM units WHERE id=? AND enabled=1",(int(p[1]),));
        if not row: await message.answer("Юнит не найден."); return
        battles=[b for b in runtime.battles.values() if message.from_user.id in b.members and not b.stop_event.is_set()]
        if not battles: await message.answer("Активный бой не найден."); return
        ok,text=await battles[0].place(message.from_user.id,dict(row),pos); await message.answer(("✅ " if ok else "❌ ")+text)

    @child.message(Command("lobby"))
    @child_only
    async def lobby(message:Message,runtime:Runtime):
        await runtime.db.player(message.from_user.id,message.from_user.username or '',message.from_user.first_name or ''); row=await runtime.db.one("SELECT id FROM maps WHERE enabled=1 ORDER BY id LIMIT 1");
        if not row: await message.answer("Карт нет."); return
        code=''.join(secrets.choice(string.ascii_uppercase+string.digits) for _ in range(6)); await runtime.db.exec("INSERT INTO lobbies(code,map_id,owner_id,max_players,created_at) VALUES(?,?,?,?,?)",(code,int(row[0]),message.from_user.id,PLANS[runtime.plan].lobby,now())); await runtime.db.exec("INSERT INTO lobby_members(code,user_id,joined_at) VALUES(?,?,?)",(code,message.from_user.id,now())); await message.answer(f"🏠 Лобби: <code>{code}</code>\n/join {code}")

    @child.message(Command("join"))
    @child_only
    async def join(message:Message,runtime:Runtime):
        p=(message.text or '').split(maxsplit=1);
        if len(p)!=2: await message.answer("/join CODE"); return
        await runtime.db.player(message.from_user.id,message.from_user.username or '',message.from_user.first_name or ''); row=await runtime.db.one("SELECT * FROM lobbies WHERE code=?",(p[1].upper(),));
        if not row: await message.answer("Лобби не найдено."); return
        count=int(await runtime.db.scalar("SELECT COUNT(*) FROM lobby_members WHERE code=?",(p[1].upper(),)) or 0)
        if count>=int(row['max_players']): await message.answer("Лобби заполнено."); return
        await runtime.db.exec("INSERT OR IGNORE INTO lobby_members(code,user_id,joined_at) VALUES(?,?,?)",(p[1].upper(),message.from_user.id,now())); await message.answer("✅ Ты в лобби.")

    @child.message(Command("export_csv"))
    @child_only
    async def export_csv(message:Message,runtime:Runtime):
        if not PLANS[runtime.plan].csv: await message.answer("❌ CSV доступен с Coins и выше."); return
        rows=await runtime.db.all("SELECT * FROM units ORDER BY id"); out=io.StringIO(); fields=['id','name','cost','damage','attack_speed','attack_range','attack_type','target_type','splash','slow','slow_duration','stun_duration','burn_damage','burn_duration','income','enabled']; w=csv.writer(out); w.writerow(fields)
        for r in rows:w.writerow([r[x] for x in fields])
        await message.answer_document(BufferedInputFile(out.getvalue().encode('utf-8-sig'),filename='units.csv'))

    @child.message(Command("import_units"))
    @child_only
    async def import_units(message:Message,state:FSMContext,runtime:Runtime):
        if not PLANS[runtime.plan].csv: await message.answer("❌ CSV доступен с Coins и выше."); return
        await state.set_state(ImportState.waiting); await message.answer("Пришли CSV-файл.")

    @child.message(ImportState.waiting,F.document)
    @child_only
    async def import_csv(message:Message,state:FSMContext,runtime:Runtime):
        await state.clear(); buf=io.BytesIO(); await message.bot.download(message.document,destination=buf); buf.seek(0); reader=csv.DictReader(io.TextIOWrapper(buf,encoding='utf-8-sig')); added=0
        for r in reader:
            if PLANS[runtime.plan].units is not None and int(await runtime.db.scalar("SELECT COUNT(*) FROM units WHERE enabled=1") or 0)>=PLANS[runtime.plan].units: break
            if not (r.get('name') or '').strip(): continue
            await runtime.db.exec("INSERT INTO units(name,cost,damage,attack_speed,attack_range,attack_type,target_type,splash,slow,slow_duration,stun_duration,burn_damage,burn_duration,income,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(r['name'],int(r.get('cost',100)),float(r.get('damage',10)),float(r.get('attack_speed',1)),float(r.get('attack_range',20)),r.get('attack_type','Одиночный'),r.get('target_type','Универсал'),float(r.get('splash',0)),float(r.get('slow',0)),float(r.get('slow_duration',0)),float(r.get('stun_duration',0)),float(r.get('burn_damage',0)),float(r.get('burn_duration',0)),int(r.get('income',0)),now())); added+=1
        await message.answer(f"✅ Импортировано: {added}")

    @child.message(Command("unit_add"))
    @child_only
    async def unit_add(message:Message,runtime:Runtime):
        if message.from_user.id!=runtime.owner_id: await message.answer("⛔ Только владелец."); return
        p=(message.text or '').split();
        if len(p)<8: await message.answer("/unit_add NAME COST DAMAGE ATTACK_SPEED RANGE ATTACK_TYPE TARGET_TYPE"); return
        await manager.limit(runtime,'units',int(await runtime.db.scalar("SELECT COUNT(*) FROM units WHERE enabled=1") or 0)); name,cost,dmg,spd,rng,atype,ttype=p[1:8]; await runtime.db.exec("INSERT INTO units(name,cost,damage,attack_speed,attack_range,attack_type,target_type,created_at) VALUES(?,?,?,?,?,?,?,?)",(name,int(cost),float(dmg),float(spd),float(rng),atype,ttype,now())); await message.answer("✅ Юнит создан.")

    @child.message(Command("mob_add"))
    @child_only
    async def mob_add(message:Message,runtime:Runtime):
        if message.from_user.id!=runtime.owner_id: await message.answer("⛔ Только владелец."); return
        p=(message.text or '').split();
        if len(p)<7: await message.answer("/mob_add NAME HP SPEED REWARD BASE_DAMAGE TRAITS"); return
        await runtime.db.exec("INSERT INTO mobs(name,hp,speed,reward,base_damage,traits,created_at) VALUES(?,?,?,?,?,?,?)",(p[1],float(p[2]),float(p[3]),int(p[4]),int(p[5]),json.dumps([x for x in p[6].split(',') if x]),now())); await message.answer("✅ Моб создан.")

    @child.message(Command("map_add"))
    @child_only
    async def map_add(message:Message,runtime:Runtime):
        if message.from_user.id!=runtime.owner_id: await message.answer("⛔ Только владелец."); return
        p=(message.text or '').split(maxsplit=4);
        if len(p)!=5: await message.answer("/map_add NAME MAX_WAVES START_HP WAVE_JSON"); return
        await manager.limit(runtime,'maps',int(await runtime.db.scalar("SELECT COUNT(*) FROM maps WHERE enabled=1") or 0)); json.loads(p[4]); await runtime.db.exec("INSERT INTO maps(name,max_waves,start_hp,waves,created_at) VALUES(?,?,?,?,?)",(p[1],int(p[2]),int(p[3]),p[4],now())); await message.answer("✅ Карта создана.")

    @child.message(Command("banner_add"))
    @child_only
    async def banner_add(message:Message,runtime:Runtime):
        if message.from_user.id!=runtime.owner_id: await message.answer("⛔ Только владелец."); return
        if not PLANS[runtime.plan].banners: await message.answer("❌ Ротация баннеров доступна с Stars Lite."); return
        text=(message.text or '').split(maxsplit=1);
        if len(text)!=2: await message.answer("/banner_add TEXT"); return
        await runtime.db.exec("INSERT INTO banners(text,created_at) VALUES(?,?)",(text[1],now())); await message.answer("✅ Баннер добавлен.")

    @child.message(Command("child_broadcast"))
    @child_only
    async def child_broadcast(message:Message,state:FSMContext,runtime:Runtime):
        if message.from_user.id!=runtime.owner_id: await message.answer("⛔ Только владелец."); return
        await state.set_state(TextState.waiting); await message.answer("Пришли текст рассылки игрокам.")

    @child.message(TextState.waiting)
    @child_only
    async def child_broadcast_text(message:Message,state:FSMContext,runtime:Runtime):
        if message.from_user.id!=runtime.owner_id:return
        await state.clear(); rows=await runtime.db.all("SELECT user_id FROM players ORDER BY user_id"); cap=None if PLANS[runtime.plan].unlimited_broadcast else 1000; rows=rows[:cap] if cap else rows; sent=0
        for r in rows:
            try: await runtime.bot.send_message(int(r[0]),f"📢 {message.text or ''}"); sent+=1
            except TelegramAPIError: pass
        await message.answer(f"✅ Отправлено: {sent}/{len(rows)}")

    @child.message(Command("td_admin"))
    @child_only
    async def td_admin(message:Message,runtime:Runtime):
        if message.from_user.id!=runtime.owner_id: await message.answer("⛔ Только владелец."); return
        await message.answer("<b>Child Admin</b>\n\n/start /stop /restart управляются из root-команд. Здесь доступны редактирование контента, CSV и рассылки.",reply_markup=child_admin_keyboard())

    @child.callback_query(F.data.startswith("child:"))
    @child_only
    async def child_control(call:CallbackQuery,runtime:Runtime):
        if call.from_user.id!=runtime.owner_id: await call.answer("Только владелец",show_alert=True); return
        action=call.data.split(":")[1]
        if action=='start': await manager.start(runtime.bot_id)
        elif action=='stop': await manager.stop(runtime.bot_id)
        elif action=='restart': await manager.restart(runtime.bot_id)
        elif action=='status': await call.message.answer(f"@{runtime.username}\nТариф: {PLANS[runtime.plan].name}\nДо: {expiry(runtime.expires_at)}\nИгроков: {await runtime.db.scalar('SELECT COUNT(*) FROM players')}\nБоёв: {len(runtime.battles)}")
        await call.answer("OK")

    dp.include_router(root); dp.include_router(child); return dp


async def main()->None:
    settings=Settings.load(); logging.basicConfig(level=getattr(logging,settings.log_level,logging.INFO),format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    root_bot=Bot(settings.root_bot_token,default=DefaultBotProperties(parse_mode=ParseMode.HTML)); root_me=await root_bot.get_me(); vault=TokenVault(settings.encryption_key); dp=Dispatcher(); manager=Manager(dp,settings,vault,int(root_me.id)); role=RoleMiddleware(manager,int(root_me.id)); dp.message.middleware(role); dp.callback_query.middleware(role); dp.pre_checkout_query.middleware(role)
    # Rebuild router on the configured dispatcher; middleware is installed only once here.
    dp=await build_dispatcher(manager,int(root_me.id)); await manager.open()
    try: await dp.start_polling(root_bot,handle_signals=False,close_bot_session=False)
    finally: await manager.close(); await root_bot.session.close()


if __name__=="__main__": asyncio.run(main())
