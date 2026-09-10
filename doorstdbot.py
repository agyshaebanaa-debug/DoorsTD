from __future__ import annotations

"""Standalone TD Bot-as-a-Service application for aiogram 3.x."""
import asyncio, csv, io, json, logging, os, secrets, string, time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
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
UTC=timezone.utc; log=logging.getLogger("doortstdbot")
@dataclass(frozen=True,slots=True)
class Settings:
    root_bot_token:str; root_admin_id:int; encryption_key:str; data_dir:Path; log_level:str="INFO"
    @classmethod
    def load(cls):
        t=os.getenv("ROOT_BOT_TOKEN","").strip(); a=os.getenv("ROOT_ADMIN_ID","").strip(); k=os.getenv("TOKEN_ENCRYPTION_KEY","").strip()
        if not t or not a.isdigit() or not k: raise RuntimeError("ROOT_BOT_TOKEN, numeric ROOT_ADMIN_ID and TOKEN_ENCRYPTION_KEY are required")
        d=Path(os.getenv("DATA_DIR","./data")).resolve(); d.mkdir(parents=True,exist_ok=True); return cls(t,int(a),k,d,os.getenv("LOG_LEVEL","INFO").upper())
class PlanCode(StrEnum): FREE="free"; COINS="coins"; STARS_LITE="stars_lite"; STUDIO="studio"
@dataclass(frozen=True,slots=True)
class Plan:
    code:PlanCode; name:str; price:str; players:int|None; battles:int|None; units:int|None; maps:int|None; lobby:int; csv:bool; banners:bool; unlimited_broadcast:bool
PLANS={
 PlanCode.FREE:Plan(PlanCode.FREE,"🟢 Free","0",5000,25,40,15,4,False,False,False),
 PlanCode.COINS:Plan(PlanCode.COINS,"🟡 Coins","4 000 монет / 30 дней",15000,60,100,35,4,True,False,False),
 PlanCode.STARS_LITE:Plan(PlanCode.STARS_LITE,"⭐ Stars Lite","50 Stars / 30 дней",40000,120,250,70,4,True,True,False),
 PlanCode.STUDIO:Plan(PlanCode.STUDIO,"👑 Studio Ultra","150 Stars или 20 000 монет / 30 дней",None,None,None,None,6,True,True,True),
}
class TokenVault:
    def __init__(self,key): self.cipher=Fernet(key.encode())
    def encrypt(self,token): return self.cipher.encrypt(token.encode()).decode()
    def decrypt(self,value): return self.cipher.decrypt(value.encode()).decode()
def now(): return datetime.now(UTC).timestamp()
def expiry(ts): return "∞" if ts is None else datetime.fromtimestamp(ts,UTC).strftime("%Y-%m-%d %H:%M UTC")
PLATFORM_SCHEMA='''PRAGMA journal_mode=WAL;CREATE TABLE IF NOT EXISTS child_bots(id INTEGER PRIMARY KEY AUTOINCREMENT,telegram_bot_id INTEGER UNIQUE NOT NULL,owner_id INTEGER NOT NULL,username TEXT NOT NULL,token_enc TEXT NOT NULL,plan TEXT NOT NULL DEFAULT 'free',expires_at REAL,enabled INTEGER NOT NULL DEFAULT 1,created_at REAL NOT NULL,updated_at REAL NOT NULL);CREATE INDEX IF NOT EXISTS idx_owner ON child_bots(owner_id);CREATE TABLE IF NOT EXISTS wallets(user_id INTEGER PRIMARY KEY,coins INTEGER NOT NULL DEFAULT 0,updated_at REAL NOT NULL);CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,actor_id INTEGER NOT NULL,action TEXT NOT NULL,bot_id INTEGER,payload TEXT,created_at REAL NOT NULL);'''
CHILD_SCHEMA='''PRAGMA journal_mode=WAL;CREATE TABLE IF NOT EXISTS players(user_id INTEGER PRIMARY KEY,username TEXT NOT NULL DEFAULT '',first_name TEXT NOT NULL DEFAULT '',level INTEGER NOT NULL DEFAULT 1,exp INTEGER NOT NULL DEFAULT 0,base_hp INTEGER NOT NULL DEFAULT 100,created_at REAL NOT NULL,updated_at REAL NOT NULL);CREATE TABLE IF NOT EXISTS balances(user_id INTEGER PRIMARY KEY,coins INTEGER NOT NULL DEFAULT 500);CREATE TABLE IF NOT EXISTS units(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,cost INTEGER NOT NULL,damage REAL NOT NULL,attack_speed REAL NOT NULL,attack_range REAL NOT NULL,attack_type TEXT NOT NULL,target_type TEXT NOT NULL,splash REAL NOT NULL DEFAULT 0,slow REAL NOT NULL DEFAULT 0,slow_duration REAL NOT NULL DEFAULT 0,stun_duration REAL NOT NULL DEFAULT 0,burn_damage REAL NOT NULL DEFAULT 0,burn_duration REAL NOT NULL DEFAULT 0,income INTEGER NOT NULL DEFAULT 0,enabled INTEGER NOT NULL DEFAULT 1,created_at REAL NOT NULL);CREATE TABLE IF NOT EXISTS mobs(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,hp REAL NOT NULL,speed REAL NOT NULL,reward INTEGER NOT NULL,base_damage INTEGER NOT NULL DEFAULT 1,traits TEXT NOT NULL DEFAULT '[]',enabled INTEGER NOT NULL DEFAULT 1,created_at REAL NOT NULL);CREATE TABLE IF NOT EXISTS maps(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,max_waves INTEGER NOT NULL,start_hp INTEGER NOT NULL DEFAULT 100,waves TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,created_at REAL NOT NULL);CREATE TABLE IF NOT EXISTS inventories(user_id INTEGER NOT NULL,unit_id INTEGER NOT NULL,quantity INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(user_id,unit_id));CREATE TABLE IF NOT EXISTS decks(user_id INTEGER PRIMARY KEY,units TEXT NOT NULL DEFAULT '[]');CREATE TABLE IF NOT EXISTS banners(id INTEGER PRIMARY KEY AUTOINCREMENT,text TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,created_at REAL NOT NULL);CREATE TABLE IF NOT EXISTS lobbies(code TEXT PRIMARY KEY,map_id INTEGER NOT NULL,owner_id INTEGER NOT NULL,max_players INTEGER NOT NULL,created_at REAL NOT NULL);CREATE TABLE IF NOT EXISTS lobby_members(code TEXT NOT NULL,user_id INTEGER NOT NULL,joined_at REAL NOT NULL,PRIMARY KEY(code,user_id));'''
class DB:
    def __init__(self,path,schema): self.path=path; self.schema=schema; self.conn=None
    async def open(self):
        if self.conn:return
        self.path.parent.mkdir(parents=True,exist_ok=True); self.conn=await aiosqlite.connect(self.path); self.conn.row_factory=aiosqlite.Row; await self.conn.executescript(self.schema); await self.conn.commit()
    async def close(self):
        if self.conn: await self.conn.close(); self.conn=None
    def c(self):
        if self.conn is None: raise RuntimeError("database is closed")
        return self.conn
    async def exec(self,sql,params=()): await self.c().execute(sql,params); await self.c().commit()
    async def one(self,sql,params=()):
        cur=await self.c().execute(sql,params); return await cur.fetchone()
    async def all(self,sql,params=()):
        cur=await self.c().execute(sql,params); return await cur.fetchall()
    async def scalar(self,sql,params=()):
        r=await self.one(sql,params); return r[0] if r else None
class ChildDB(DB):
    async def seed(self):
        if not int(await self.scalar("SELECT COUNT(*) FROM units") or 0):
            for u in [("Scout",100,18,1.2,30,"Одиночный","Универсал",0,0,0,0,0,0,0),("Cannon",250,55,2.2,24,"Сплеш","Наземный",8,0,0,0,0,0,0),("Frost",300,22,1.5,28,"Замедление","Универсал",0,.35,2.5,0,0,0,0),("Stunner",400,36,2.8,26,"Оглушение","Универсал",0,0,0,1.3,0,0,0),("Burner",350,30,1.7,27,"Горение","Универсал",0,0,0,0,10,3,0),("Generator",200,6,4,18,"Ферма","Наземный",0,0,0,0,0,0,20)]: await self.exec("INSERT INTO units(name,cost,damage,attack_speed,attack_range,attack_type,target_type,splash,slow,slow_duration,stun_duration,burn_damage,burn_duration,income,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(*u,now()))
        if not int(await self.scalar("SELECT COUNT(*) FROM mobs") or 0):
            for m in [("Grunt",80,8,12,1,["Обычный"]),("Runner",45,14,15,1,["Обычный"]),("Air Scout",70,9,18,2,["Летающий"]),("Cloaked",120,7,24,2,["Камуфляж"]),("Titan",800,3.5,100,8,["Обычный"])]: await self.exec("INSERT INTO mobs(name,hp,speed,reward,base_damage,traits,created_at) VALUES(?,?,?,?,?,?,?)",(m[0],m[1],m[2],m[3],m[4],json.dumps(m[5],ensure_ascii=False),now()))
        if not int(await self.scalar("SELECT COUNT(*) FROM maps") or 0):
            ids=[int(r[0]) for r in await self.all("SELECT id FROM mobs ORDER BY id")]; w=[]
            for n in range(1,11):
                w.append({"wave":n,"mob":ids[(n-1)%len(ids)],"count":4+n})
                if n>=4:w.append({"wave":n,"mob":ids[2],"count":1+n//4})
                if n==10:w.append({"wave":n,"mob":ids[-1],"count":1})
            await self.exec("INSERT INTO maps(name,max_waves,start_hp,waves,created_at) VALUES(?,?,?,?,?)",("Training Grounds",10,100,json.dumps(w),now()))
    async def player(self,uid,username='',first=''):
        r=await self.one("SELECT * FROM players WHERE user_id=?",(uid,))
        if r:
            await self.exec("UPDATE players SET username=?,first_name=?,updated_at=? WHERE user_id=?",(username,first,now(),uid)); return r
        await self.exec("INSERT INTO players(user_id,username,first_name,created_at,updated_at) VALUES(?,?,?,?,?)",(uid,username,first,now(),now())); await self.exec("INSERT INTO balances(user_id,coins) VALUES(?,500)",(uid,)); u=await self.scalar("SELECT id FROM units ORDER BY id LIMIT 1")
        if u: await self.exec("INSERT INTO inventories(user_id,unit_id,quantity) VALUES(?,?,1)",(uid,int(u))); await self.exec("INSERT INTO decks(user_id,units) VALUES(?,?)",(uid,json.dumps([int(u)])));
        return await self.one("SELECT * FROM players WHERE user_id=?",(uid,))
@dataclass(slots=True)
class Mob:
    uid:int; name:str; hp:float; speed:float; reward:int; base_damage:int; traits:tuple[str,...]; pos:float=100.; slow:float=0.; slow_until:float=0.; stun_until:float=0.; burn_damage:float=0.; burn_until:float=0.
@dataclass(slots=True)
class Tower:
    owner:int; row:dict; pos:float; cooldown:float=0.
class Battle:
    def __init__(self,bid,map_name,max_waves,base_hp,waves,members,notify): self.bid,self.map_name,self.max_waves,self.base_hp,self.waves,self.members,self.notify=bid,map_name,max_waves,base_hp,waves,members,notify; self.max_hp=base_hp; self.wave=0; self.mobs={}; self.towers=[]; self.next_uid=1; self.stop_event=asyncio.Event(); self.lock=asyncio.Lock(); self.phase='prepare'
    async def start(self):
        self.phase='wave'; await self.notify(f"⚔️ Бой <b>#{self.bid}</b> начат на <b>{self.map_name}</b>."); await self.spawn(); last=time.monotonic(); bank=0
        while not self.stop_event.is_set():
            await asyncio.sleep(.5); t=time.monotonic(); dt=min(2,t-last); last=t; bank+=dt
            async with self.lock:
                for m in list(self.mobs.values()):
                    if m.hp<=0: continue
                    if m.burn_until>t:m.hp-=m.burn_damage*dt
                    if m.hp<=0:continue
                    if m.stun_until>t:continue
                    m.pos-=m.speed*(max(.1,1-m.slow) if m.slow_until>t else 1)*dt
                    if m.pos<=0:self.base_hp-=m.base_damage;m.hp=0
                for tw in self.towers:
                    if tw.cooldown>t:continue
                    candidates=[m for m in self.mobs.values() if m.hp>0 and self.valid(tw.row,m,tw.pos)]
                    if not candidates:continue
                    target=max(candidates,key=lambda x:x.pos); hits=[target]
                    if float(tw.row['splash'])>0:hits=[m for m in candidates if abs(m.pos-target.pos)<=float(tw.row['splash'])]
                    for m in hits:
                        dmg=float(tw.row['damage'])*(2 if secrets.randbelow(100)<8 else 1);m.hp-=dmg;self.members[tw.owner]+=int(dmg*.5)
                        if float(tw.row['slow']) and float(tw.row['slow_duration']):m.slow=float(tw.row['slow']);m.slow_until=t+float(tw.row['slow_duration'])
                        if float(tw.row['stun_duration']):m.stun_until=t+float(tw.row['stun_duration'])
                        if float(tw.row['burn_damage']) and float(tw.row['burn_duration']):m.burn_damage=float(tw.row['burn_damage']);m.burn_until=t+float(tw.row['burn_duration'])
                    tw.cooldown=t+max(.05,float(tw.row['attack_speed']))
                if bank>=5:
                    bank=0
                    for tw in self.towers:
                        if int(tw.row['income']):self.members[tw.owner]+=int(tw.row['income'])
                for m in [x for x in self.mobs.values() if x.hp<=0]: self.members[next(iter(self.members))]+=m.reward; self.mobs.pop(m.uid,None)
                if self.base_hp<=0:self.phase='defeat';self.stop_event.set();await self.notify(f"💥 Бой <b>#{self.bid}</b>: поражение.");continue
                if not self.mobs:
                    if self.wave>=self.max_waves:self.phase='victory';self.stop_event.set();reward=50+self.max_waves*10;self.members.update({u:c+reward for u,c in self.members.items()});await self.notify(f"🏆 Бой <b>#{self.bid}</b>: победа. +{reward} монет каждому.")
                    else:await self.spawn()
    async def spawn(self):
        self.wave+=1
        for e in self.waves.get(self.wave,[]):
            for _ in range(max(1,int(e['count']))): self.mobs[self.next_uid]=Mob(self.next_uid,e['name'],e['hp'],e['speed'],e['reward'],e['base_damage'],tuple(e['traits']));self.next_uid+=1
        await self.notify(f"🌊 Волна <b>{self.wave}/{self.max_waves}</b>: целей {len(self.mobs)}.")
    def valid(self,row,m,pos):
        if abs(pos-m.pos)>float(row['attack_range']):return False
        kinds={x.strip() for x in row['target_type'].split(',')};tr=set(m.traits)
        if 'Летающий' in tr and not ({'Анти-Воздух','Универсал'}&kinds):return False
        if 'Камуфляж' in tr and not ({'Детектор','Универсал'}&kinds):return False
        return True
    async def place(self,uid,row,pos):
        async with self.lock:
            if uid not in self.members:return False,'Игрок не участвует.'
            if not 0<=pos<=100:return False,'Позиция 0..100.'
            if self.members[uid]<int(row['cost']):return False,'Недостаточно монет.'
            self.members[uid]-=int(row['cost']);self.towers.append(Tower(uid,row,pos));return True,'Юнит установлен.'
@dataclass(slots=True)
class Runtime:
    bot_id:int; owner_id:int; username:str; token:str; plan:PlanCode; expires_at:float|None; bot:Bot; db:ChildDB; poll_task:asyncio.Task|None=None; battles:dict[str,Battle]=field(default_factory=dict); temp_cache:dict[str,object]=field(default_factory=dict)
    @property
    def running(self):return self.poll_task is not None and not self.poll_task.done()
class Manager:
    def __init__(self,dp,settings,vault):self.dp=dp;self.settings=settings;self.vault=vault;self.platform=DB(settings.data_dir/'platform.db',PLATFORM_SCHEMA);self.runtimes={};self.lock=asyncio.Lock();self.expiry_task=None
    async def open(self):
        await self.platform.open();await self.sync_expiry()
        for r in await self.platform.all("SELECT telegram_bot_id FROM child_bots WHERE enabled=1"):
            try:await self.start(int(r[0]))
            except Exception:log.exception('restore child failed')
        self.expiry_task=asyncio.create_task(self.expiry_loop(),name='expiry-loop')
    async def close(self):
        if self.expiry_task:self.expiry_task.cancel();await asyncio.gather(self.expiry_task,return_exceptions=True)
        for b in list(self.runtimes):await self.stop(b)
        await self.platform.close()
    async def expiry_loop(self):
        while True:await asyncio.sleep(60);await self.sync_expiry()
    async def sync_expiry(self):
        for r in await self.platform.all("SELECT telegram_bot_id FROM child_bots WHERE plan!='free' AND expires_at IS NOT NULL AND expires_at<=?",(now(),)):
            bid=int(r[0]);await self.platform.exec("UPDATE child_bots SET plan='free',expires_at=NULL,updated_at=? WHERE telegram_bot_id=?",(now(),bid));rt=self.runtimes.get(bid)
            if rt:rt.plan=PlanCode.FREE;rt.expires_at=None;rt.temp_cache.clear()
    async def child_db(self,bid):db=ChildDB(self.settings.data_dir/f'child_{bid}.db',CHILD_SCHEMA);await db.open();await db.seed();return db
    async def register(self,owner,token):
        if not token or len(token)<20:raise ValueError('Некорректный токен BotFather.')
        probe=Bot(token,default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        try:me=await probe.get_me()
        finally:await probe.session.close()
        if await self.platform.one("SELECT 1 FROM child_bots WHERE telegram_bot_id=?",(int(me.id),)):raise ValueError('Этот бот уже подключён.')
        await self.platform.exec("INSERT INTO child_bots(telegram_bot_id,owner_id,username,token_enc,plan,expires_at,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",(int(me.id),owner,me.username or str(me.id),self.vault.encrypt(token),'free',None,1,now(),now()));await self.platform.exec("INSERT INTO wallets(user_id,coins,updated_at) VALUES(?,?,?) ON CONFLICT(user_id) DO NOTHING",(owner,0,now()));return await self.build(int(me.id))
    async def build(self,bid):
        if bid in self.runtimes:return self.runtimes[bid]
        row=await self.platform.one("SELECT * FROM child_bots WHERE telegram_bot_id=?",(bid,));
        if not row:raise ValueError('Дочерний бот не найден.')
        token=self.vault.decrypt(row['token_enc']);db=await self.child_db(bid);rt=Runtime(bid,int(row['owner_id']),row['username'],token,PlanCode(row['plan']),row['expires_at'],Bot(token,default=DefaultBotProperties(parse_mode=ParseMode.HTML)),db);self.runtimes[bid]=rt;return rt
    async def poll(self,rt):
        offset=0;allowed=self.dp.resolve_used_update_types();await rt.bot.delete_webhook(drop_pending_updates=False)
        while True:
            try:
                for update in await rt.bot.get_updates(offset=offset,timeout=25,allowed_updates=allowed):offset=update.update_id+1;await self.dp.feed_update(rt.bot,update)
            except asyncio.CancelledError:raise
            except TelegramUnauthorizedError:await self.platform.exec("UPDATE child_bots SET enabled=0,updated_at=? WHERE telegram_bot_id=?",(now(),rt.bot_id));break
            except TelegramAPIError:log.exception('child telegram error');await asyncio.sleep(2)
            except Exception:log.exception('child polling error');await asyncio.sleep(2)
    async def start(self,bid):
        async with self.lock:
            rt=await self.build(bid)
            if rt.running:return rt
            await self.platform.exec("UPDATE child_bots SET enabled=1,updated_at=? WHERE telegram_bot_id=?",(now(),bid));rt.temp_cache.clear();rt.poll_task=asyncio.create_task(self.poll(rt),name=f'child-{bid}');return rt
    async def stop(self,bid):
        async with self.lock:
            rt=self.runtimes.get(bid);await self.platform.exec("UPDATE child_bots SET enabled=0,updated_at=? WHERE telegram_bot_id=?",(now(),bid))
            if not rt:return
            if rt.poll_task and not rt.poll_task.done():rt.poll_task.cancel();await asyncio.gather(rt.poll_task,return_exceptions=True)
            for b in rt.battles.values():b.stop_event.set()
            rt.battles.clear();rt.poll_task=None;rt.temp_cache.clear();await rt.bot.session.close();await rt.db.close();self.runtimes.pop(bid,None)
    async def restart(self,bid):await self.stop(bid);return await self.start(bid)
    async def set_plan(self,bid,plan,days,actor):
        exp=None if plan==PlanCode.FREE else now()+days*86400;await self.platform.exec("UPDATE child_bots SET plan=?,expires_at=?,updated_at=? WHERE telegram_bot_id=?",(plan.value,exp,now(),bid));rt=self.runtimes.get(bid)
        if rt:rt.plan=plan;rt.expires_at=exp;rt.temp_cache.clear()
        await self.platform.exec("INSERT INTO events(actor_id,action,bot_id,payload,created_at) VALUES(?,?,?,?,?)",(actor,'plan',bid,json.dumps({'plan':plan.value,'days':days}),now()))
    async def add_days(self,bid,days):
        row=await self.platform.one("SELECT plan,expires_at FROM child_bots WHERE telegram_bot_id=?",(bid,));
        if not row:raise ValueError('Бот не найден')
        if row['plan']=='free':raise ValueError('Для Free сначала установите платный тариф.')
        exp=float(row['expires_at'] or now())+days*86400;await self.platform.exec("UPDATE child_bots SET expires_at=?,updated_at=? WHERE telegram_bot_id=?",(exp,now(),bid));rt=self.runtimes.get(bid)
        if rt:rt.expires_at=exp
    async def limit(self,rt,attr,current):
        lim=getattr(PLANS[rt.plan],attr)
        if lim is not None and current>=lim:raise ValueError(f'Лимит тарифа {PLANS[rt.plan].name}: {lim}')
class RoleMiddleware(BaseMiddleware):
    def __init__(self,manager,root_id):self.manager,self.root_id=manager,root_id
    async def __call__(self,handler,event,data):
        bot=data['bot'];data['is_root']=bot.id==self.root_id;data['is_child']=bot.id!=self.root_id and bot.id in self.manager.runtimes
        if data['is_child']:data['runtime']=self.manager.runtimes[bot.id]
        return await handler(event,data)
class TokenState(StatesGroup):waiting=State()
class TextState(StatesGroup):waiting=State()
class ImportState(StatesGroup):waiting=State()
def root_only(fn):
    async def w(event,data):return await fn(event,data) if data.get('is_root') else None
    return w
def child_only(fn):
    async def w(event,data):return await fn(event,data) if data.get('is_child') else None
    return w
def kb_root():return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='🤖 Мои боты',callback_data='root:mybots')],[InlineKeyboardButton(text='➕ Добавить',callback_data='root:add')],[InlineKeyboardButton(text='💳 Тарифы',callback_data='root:plans')],[InlineKeyboardButton(text='📡 Сеть',callback_data='root:network')]])
def kb_child():return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='▶️ Start',callback_data='child:start'),InlineKeyboardButton(text='⏸ Stop',callback_data='child:stop')],[InlineKeyboardButton(text='🔄 Restart',callback_data='child:restart')],[InlineKeyboardButton(text='📊 Status',callback_data='child:status')]])
async def make_dispatcher(manager,root_id):
    dp=Dispatcher();role=RoleMiddleware(manager,root_id);dp.message.middleware(role);dp.callback_query.middleware(role);dp.pre_checkout_query.middleware(role);root=Router();child=Router()
    @root.message(CommandStart())
    @root_only
    async def rs(m:Message):await m.answer('<b>TD Bot-as-a-Service</b>\n/addbot · /mybots · /plans · /wallet · /admin',reply_markup=kb_root())
    @root.message(Command('addbot'))
    @root_only
    async def ra(m:Message,state:FSMContext):await state.set_state(TokenState.waiting);await m.answer('Пришли токен из @BotFather. Сообщение с токеном будет удалено после проверки.')
    @root.message(TokenState.waiting)
    @root_only
    async def rat(m:Message,state:FSMContext):
        try:rt=await manager.register(m.from_user.id,(m.text or '').strip());await manager.start(rt.bot_id);await state.clear();
        except Exception as e:await m.answer(f'❌ {str(e)[:500]}');return
        try:await m.delete()
        except TelegramAPIError:pass
        await m.answer(f'✅ Подключён @{rt.username}. BOT_ID=<code>{rt.bot_id}</code>')
    @root.message(Command('mybots'))
    @root_only
    async def rm(m:Message):
        rows=await manager.platform.all('SELECT * FROM child_bots WHERE owner_id=? ORDER BY id DESC',(m.from_user.id,));await m.answer('\n'.join(f"{'🟢' if manager.runtimes.get(int(r['telegram_bot_id'])) else '⛔'} @{r['username']} · {PLANS[PlanCode(r['plan'])].name} · до {expiry(r['expires_at'])}\n/buy {r['telegram_bot_id']} coins|stars_lite|studio_stars|studio_coins" for r in rows) or 'Ботов пока нет.',reply_markup=kb_root())
    @root.message(Command('wallet'))
    @root_only
    async def rw(m:Message):
        r=await manager.platform.one('SELECT coins FROM wallets WHERE user_id=?',(m.from_user.id,));await m.answer(f"💰 Root-кошелёк: <b>{int(r[0]) if r else 0}</b>")
    @root.message(Command('plans'))
    @root_only
    async def rp(m:Message):await m.answer('\n'.join(f'{p.name}: {p.price}; players={p.players or "∞"}, battles={p.battles or "∞"}, units={p.units or "∞"}, maps={p.maps or "∞"}, lobby={p.lobby}' for p in PLANS.values())+'\n\nПокупка: /buy BOT_ID coins|stars_lite|studio_stars|studio_coins')
    @root.message(Command('buy'))
    @root_only
    async def rb(m:Message):
        p=(m.text or '').split();
        if len(p)!=3 or not p[1].isdigit() or p[2] not in {'coins','stars_lite','studio_stars','studio_coins'}:await m.answer('/buy BOT_ID coins|stars_lite|studio_stars|studio_coins');return
        bid,mode=int(p[1]),p[2];r=await manager.platform.one('SELECT * FROM child_bots WHERE telegram_bot_id=? AND owner_id=?',(bid,m.from_user.id));
        if not r:await m.answer('⛔ Бот не найден или не твой.');return
        if mode in {'coins','studio_coins'}:
            price=4000 if mode=='coins' else 20000;plan=PlanCode.COINS if mode=='coins' else PlanCode.STUDIO;w=await manager.platform.one('SELECT coins FROM wallets WHERE user_id=?',(m.from_user.id,));bal=int(w[0]) if w else 0
            if bal<price:await m.answer(f'Недостаточно монет. Баланс: {bal}');return
            await manager.platform.exec('UPDATE wallets SET coins=coins-?,updated_at=? WHERE user_id=?',(price,now(),m.from_user.id));await manager.set_plan(bid,plan,30,m.from_user.id);await m.answer(f'✅ {PLANS[plan].name} активирован на 30 дней.')
        else:
            stars=50 if mode=='stars_lite' else 150;plan=PlanCode.STARS_LITE if stars==50 else PlanCode.STUDIO;await m.bot.send_invoice(chat_id=m.from_user.id,title='TD Platform',description=f'{PLANS[plan].name} на 30 дней',payload=f'sub:{bid}:{mode}:{m.from_user.id}',currency='XTR',prices=[LabeledPrice(label='Telegram Stars',amount=stars)],provider_token='')
    @root.pre_checkout_query()
    @root_only
    async def pc(q:PreCheckoutQuery):
        try:_,bid,mode,uid=q.invoice_payload.split(':');r=await manager.platform.one('SELECT owner_id FROM child_bots WHERE telegram_bot_id=?',(int(bid),));ok=r is not None and int(r['owner_id'])==q.from_user.id and int(uid)==q.from_user.id and mode in {'stars_lite','studio_stars'};await q.answer(ok=ok,error_message=None if ok else 'Платёж не прошёл проверку')
        except Exception:await q.answer(ok=False,error_message='Некорректный платёж')
    @root.message(F.successful_payment)
    @root_only
    async def paid(m:Message):
        try:_,bid,mode,uid=m.successful_payment.invoice_payload.split(':');
        except ValueError:return
        if int(uid)!=m.from_user.id:return
        plan=PlanCode.STARS_LITE if mode=='stars_lite' else PlanCode.STUDIO;await manager.set_plan(int(bid),plan,30,m.from_user.id);await m.answer(f'✅ {PLANS[plan].name} активирован на 30 дней.')
    @root.message(Command('admin'))
    @root_only
    async def admin(m:Message):
        if m.from_user.id!=manager.settings.root_admin_id:await m.answer('⛔ Нет доступа.');return
        await m.answer('<b>ROOT ADMIN</b>\n/network · /broadcast · /set_plan · /add_days · /kill · /delete_bot · /grant_coins')
    @root.message(Command('network'))
    @root_only
    async def network(m:Message):
        if m.from_user.id!=manager.settings.root_admin_id:await m.answer('⛔ Нет доступа.');return
        out=['<b>🤖 Сеть дочерних ботов</b>']
        for r in await manager.platform.all('SELECT * FROM child_bots ORDER BY id DESC'):
            bid=int(r['telegram_bot_id']);rt=manager.runtimes.get(bid);db=rt.db if rt else await manager.child_db(bid);users=int(await db.scalar('SELECT COUNT(*) FROM players') or 0);battles=len(rt.battles) if rt else 0
            if not rt:await db.close()
            out.append(f"{'🟢' if rt else '⛔'} @{r['username']} · owner={r['owner_id']} · {PLANS[PlanCode(r['plan'])].name} · до {expiry(r['expires_at'])} · players={users} · battles={battles}")
        await m.answer('\n'.join(out))
    @root.message(Command('set_plan'))
    @root_only
    async def sp(m:Message):
        if m.from_user.id!=manager.settings.root_admin_id:await m.answer('⛔ Нет доступа.');return
        p=(m.text or '').split();
        if len(p)!=4 or not p[1].isdigit() or p[2] not in {x.value for x in PlanCode} or not p[3].isdigit():await m.answer('/set_plan BOT_ID free|coins|stars_lite|studio DAYS');return
        await manager.set_plan(int(p[1]),PlanCode(p[2]),int(p[3]),m.from_user.id);await m.answer('✅ Тариф установлен.')
    @root.message(Command('add_days'))
    @root_only
    async def ad(m:Message):
        if m.from_user.id!=manager.settings.root_admin_id:await m.answer('⛔ Нет доступа.');return
        p=(m.text or '').split();
        if len(p)!=3 or not p[1].isdigit() or not p[2].isdigit():await m.answer('/add_days BOT_ID DAYS');return
        try:await manager.add_days(int(p[1]),int(p[2]));await m.answer('✅ Дни добавлены.')
        except ValueError as e:await m.answer(f'❌ {e}')
    @root.message(Command('kill'))
    @root_only
    async def kl(m:Message):
        if m.from_user.id!=manager.settings.root_admin_id:await m.answer('⛔ Нет доступа.');return
        p=(m.text or '').split();
        if len(p)==2 and p[1].isdigit():await manager.stop(int(p[1]));await m.answer('🛑 Kill switch выполнен.')
    @root.message(Command('delete_bot'))
    @root_only
    async def dl(m:Message):
        if m.from_user.id!=manager.settings.root_admin_id:await m.answer('⛔ Нет доступа.');return
        p=(m.text or '').split();
        if len(p)!=2 or not p[1].isdigit():await m.answer('/delete_bot BOT_ID');return
        bid=int(p[1]);await manager.stop(bid);await manager.platform.exec('DELETE FROM child_bots WHERE telegram_bot_id=?',(bid,));(manager.settings.data_dir/f'child_{bid}.db').unlink(missing_ok=True);await m.answer('🗑 Бот удалён.')
    @root.message(Command('grant_coins'))
    @root_only
    async def gc(m:Message):
        if m.from_user.id!=manager.settings.root_admin_id:await m.answer('⛔ Нет доступа.');return
        p=(m.text or '').split();
        if len(p)!=3 or not p[1].isdigit() or not p[2].lstrip('-').isdigit():await m.answer('/grant_coins USER_ID AMOUNT');return
        await manager.platform.exec('INSERT INTO wallets(user_id,coins,updated_at) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET coins=coins+excluded.coins,updated_at=excluded.updated_at',(int(p[1]),int(p[2]),now()));await m.answer('✅ Баланс изменён.')
    @root.message(Command('broadcast'))
    @root_only
    async def br(m:Message,state:FSMContext):
        if m.from_user.id!=manager.settings.root_admin_id:await m.answer('⛔ Нет доступа.');return
        await state.set_state(TextState.waiting);await m.answer('Пришли текст рассылки по сети.')
    @root.message(TextState.waiting)
    @root_only
    async def brt(m:Message,state:FSMContext):
        if m.from_user.id!=manager.settings.root_admin_id:return
        await state.clear();sent=total=0
        for rt in list(manager.runtimes.values()):
            for r in await rt.db.all('SELECT user_id FROM players'):
                total+=1
                try:await rt.bot.send_message(int(r[0]),f'📢 <b>Системное сообщение</b>\n\n{m.text or ""}');sent+=1
                except TelegramAPIError:pass
        await m.answer(f'✅ Рассылка: {sent}/{total}')
    @root.callback_query(F.data=='root:add')
    @root_only
    async def cba(c:CallbackQuery,state:FSMContext):await state.set_state(TokenState.waiting);await c.message.answer('Пришли токен из @BotFather.');await c.answer()
    @root.callback_query(F.data.in_({'root:mybots','root:plans','root:network'}))
    @root_only
    async def cbm(c:CallbackQuery):await c.message.answer('Используй /mybots, /plans или /network.');await c.answer()
    @child.message(CommandStart())
    @child_only
    async def cs(m:Message,runtime:Runtime):
        existing=await runtime.db.one('SELECT user_id FROM players WHERE user_id=?',(m.from_user.id,));count=int(await runtime.db.scalar('SELECT COUNT(*) FROM players') or 0)
        if not existing and PLANS[runtime.plan].players is not None and count>=PLANS[runtime.plan].players:await m.answer(f"❌ Достигнут лимит игроков: {PLANS[runtime.plan].players}");return
        await runtime.db.player(m.from_user.id,m.from_user.username or '',m.from_user.first_name or '');p=PLANS[runtime.plan];banner=''
        if p.banners:
            rows=await runtime.db.all('SELECT text FROM banners WHERE enabled=1 ORDER BY id');
            if rows:banner=f"\n\n📣 {rows[int(time.time()//3600)%len(rows)][0]}"
        await m.answer(f"<b>@{runtime.username}</b>\nТариф: {p.name}\nИгроков: {await runtime.db.scalar('SELECT COUNT(*) FROM players')}/{p.players or '∞'}\nЮнитов: {await runtime.db.scalar('SELECT COUNT(*) FROM units WHERE enabled=1')}/{p.units or '∞'}\nКарт: {await runtime.db.scalar('SELECT COUNT(*) FROM maps WHERE enabled=1')}/{p.maps or '∞'}\n\n/profile /units /maps /battle /lobby /join CODE /place UNIT_ID POS /td_admin"+banner)
    @child.message(Command('profile'))
    @child_only
    async def prof(m:Message,runtime:Runtime):
        r=await runtime.db.player(m.from_user.id,m.from_user.username or '',m.from_user.first_name or '');c=int(await runtime.db.scalar('SELECT coins FROM balances WHERE user_id=?',(m.from_user.id,)) or 0);await m.answer(f"👤 <b>{r['first_name'] or r['username']}</b>\nУровень: {r['level']}\nEXP: {r['exp']}\nHP: {r['base_hp']}\n💰 Монеты: {c}")
    @child.message(Command('units'))
    @child_only
    async def un(m:Message,runtime:Runtime):
        rows=await runtime.db.all('SELECT * FROM units WHERE enabled=1 ORDER BY id');await m.answer('<b>Юниты</b>\n'+'\n'.join(f"#{r['id']} {r['name']} — {r['cost']}💰 · {r['attack_type']} · {r['target_type']} · dmg {r['damage']}" for r in rows)[:3900])
    @child.message(Command('maps'))
    @child_only
    async def mp(m:Message,runtime:Runtime):
        rows=await runtime.db.all('SELECT * FROM maps WHERE enabled=1 ORDER BY id');await m.answer('<b>Карты</b>\n'+'\n'.join(f"#{r['id']} {r['name']} — {r['max_waves']} волн" for r in rows))
    @child.message(Command('battle'))
    @child_only
    async def bt(m:Message,runtime:Runtime):
        rows=await runtime.db.all('SELECT * FROM maps WHERE enabled=1 ORDER BY id');await m.answer('Выбери карту:',reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"⚔️ {r['name']}",callback_data=f'battle:{r['id']}')] for r in rows]))
    @child.callback_query(F.data.startswith('battle:'))
    @child_only
    async def btc(c:CallbackQuery,runtime:Runtime):
        try:await manager.limit(runtime,'battles',len(runtime.battles))
        except ValueError as e:await c.answer(str(e),show_alert=True);return
        row=await runtime.db.one('SELECT * FROM maps WHERE id=? AND enabled=1',(int(c.data.split(':')[1]),));
        if not row:await c.answer('Карта не найдена',show_alert=True);return
        mobs={int(x['id']):x for x in await runtime.db.all('SELECT * FROM mobs WHERE enabled=1')};cfg=json.loads(row['waves']);waves={}
        for e in cfg:
            md=mobs[int(e['mob'])];waves.setdefault(int(e['wave']),[]).append({'name':md['name'],'hp':float(md['hp']),'speed':float(md['speed']),'reward':int(md['reward']),'base_damage':int(md['base_damage']),'traits':json.loads(md['traits']),'count':int(e['count'])})
        coins=int(await runtime.db.scalar('SELECT coins FROM balances WHERE user_id=?',(c.from_user.id,)) or 0);members={c.from_user.id:coins};bid=secrets.token_hex(3).upper()
        async def notify(text):
            for uid in members:
                try:await runtime.bot.send_message(uid,text)
                except TelegramAPIError:pass
        b=Battle(bid,row['name'],int(row['max_waves']),int(row['start_hp']),waves,members,notify);runtime.battles[bid]=b
        async def run():
            try:await b.start()
            finally:
                for uid,coin in members.items():await runtime.db.exec('UPDATE balances SET coins=? WHERE user_id=?',(coin,uid))
                runtime.battles.pop(bid,None)
        asyncio.create_task(run(),name=f'battle-{runtime.bot_id}-{bid}');await c.message.answer(f'🎮 Бой <code>{bid}</code> запущен. /place UNIT_ID POSITION');await c.answer()
    @child.message(Command('place'))
    @child_only
    async def pl(m:Message,runtime:Runtime):
        p=(m.text or '').split();
        if len(p)!=3 or not p[1].isdigit():await m.answer('/place UNIT_ID POSITION');return
        try:pos=float(p[2])
        except ValueError:await m.answer('POSITION должна быть числом');return
        row=await runtime.db.one('SELECT * FROM units WHERE id=? AND enabled=1',(int(p[1]),));
        if not row:await m.answer('Юнит не найден.');return
        bs=[b for b in runtime.battles.values() if m.from_user.id in b.members and not b.stop_event.is_set()];
        if not bs:await m.answer('Активный бой не найден.');return
        ok,text=await bs[0].place(m.from_user.id,dict(row),pos);await m.answer(('✅ ' if ok else '❌ ')+text)
    @child.message(Command('lobby'))
    @child_only
    async def lo(m:Message,runtime:Runtime):
        await runtime.db.player(m.from_user.id,m.from_user.username or '',m.from_user.first_name or '');row=await runtime.db.one('SELECT id FROM maps WHERE enabled=1 ORDER BY id LIMIT 1');
        if not row:await m.answer('Карт нет.');return
        code=''.join(secrets.choice(string.ascii_uppercase+string.digits) for _ in range(6));await runtime.db.exec('INSERT INTO lobbies(code,map_id,owner_id,max_players,created_at) VALUES(?,?,?,?,?)',(code,int(row[0]),m.from_user.id,PLANS[runtime.plan].lobby,now()));await runtime.db.exec('INSERT INTO lobby_members(code,user_id,joined_at) VALUES(?,?,?)',(code,m.from_user.id,now()));await m.answer(f'🏠 Лобби: <code>{code}</code>\n/join {code}')
    @child.message(Command('join'))
    @child_only
    async def jo(m:Message,runtime:Runtime):
        p=(m.text or '').split(maxsplit=1);
        if len(p)!=2:await m.answer('/join CODE');return
        await runtime.db.player(m.from_user.id,m.from_user.username or '',m.from_user.first_name or '');row=await runtime.db.one('SELECT * FROM lobbies WHERE code=?',(p[1].upper(),));
        if not row:await m.answer('Лобби не найдено.');return
        if int(await runtime.db.scalar('SELECT COUNT(*) FROM lobby_members WHERE code=?',(p[1].upper(),)) or 0)>=int(row['max_players']):await m.answer('Лобби заполнено.');return
        await runtime.db.exec('INSERT OR IGNORE INTO lobby_members(code,user_id,joined_at) VALUES(?,?,?)',(p[1].upper(),m.from_user.id,now()));await m.answer('✅ Ты в лобби.')
    @child.message(Command('export_csv'))
    @child_only
    async def ex(m:Message,runtime:Runtime):
        if not PLANS[runtime.plan].csv:await m.answer('❌ CSV доступен с Coins и выше.');return
        rows=await runtime.db.all('SELECT * FROM units ORDER BY id');f=['id','name','cost','damage','attack_speed','attack_range','attack_type','target_type','splash','slow','slow_duration','stun_duration','burn_damage','burn_duration','income','enabled'];o=io.StringIO();w=csv.writer(o);w.writerow(f)
        for r in rows:w.writerow([r[x] for x in f])
        await m.answer_document(BufferedInputFile(o.getvalue().encode('utf-8-sig'),filename='units.csv'))
    @child.message(Command('import_units'))
    @child_only
    async def ix(m:Message,state:FSMContext,runtime:Runtime):
        if not PLANS[runtime.plan].csv:await m.answer('❌ CSV доступен с Coins и выше.');return
        await state.set_state(ImportState.waiting);await m.answer('Пришли CSV-файл.')
    @child.message(ImportState.waiting,F.document)
    @child_only
    async def ixf(m:Message,state:FSMContext,runtime:Runtime):
        await state.clear();buf=io.BytesIO();await m.bot.download(m.document,destination=buf);buf.seek(0);reader=csv.DictReader(io.TextIOWrapper(buf,encoding='utf-8-sig'));added=0
        for r in reader:
            if PLANS[runtime.plan].units is not None and int(await runtime.db.scalar('SELECT COUNT(*) FROM units WHERE enabled=1') or 0)>=PLANS[runtime.plan].units:break
            if not (r.get('name') or '').strip():continue
            await runtime.db.exec('INSERT INTO units(name,cost,damage,attack_speed,attack_range,attack_type,target_type,splash,slow,slow_duration,stun_duration,burn_damage,burn_duration,income,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(r['name'],int(r.get('cost',100)),float(r.get('damage',10)),float(r.get('attack_speed',1)),float(r.get('attack_range',20)),r.get('attack_type','Одиночный'),r.get('target_type','Универсал'),float(r.get('splash',0)),float(r.get('slow',0)),float(r.get('slow_duration',0)),float(r.get('stun_duration',0)),float(r.get('burn_damage',0)),float(r.get('burn_duration',0)),int(r.get('income',0)),now()));added+=1
        await m.answer(f'✅ Импортировано: {added}')
    @child.message(Command('unit_add'))
    @child_only
    async def ua(m:Message,runtime:Runtime):
        if m.from_user.id!=runtime.owner_id:await m.answer('⛔ Только владелец.');return
        p=(m.text or '').split();
        if len(p)<8:await m.answer('/unit_add NAME COST DAMAGE ATTACK_SPEED RANGE ATTACK_TYPE TARGET_TYPE');return
        await manager.limit(runtime,'units',int(await runtime.db.scalar('SELECT COUNT(*) FROM units WHERE enabled=1') or 0));n,c,d,s,rng,at,tt=p[1:8];await runtime.db.exec('INSERT INTO units(name,cost,damage,attack_speed,attack_range,attack_type,target_type,created_at) VALUES(?,?,?,?,?,?,?,?)',(n,int(c),float(d),float(s),float(rng),at,tt,now()));await m.answer('✅ Юнит создан.')
    @child.message(Command('mob_add'))
    @child_only
    async def ma(m:Message,runtime:Runtime):
        if m.from_user.id!=runtime.owner_id:await m.answer('⛔ Только владелец.');return
        p=(m.text or '').split();
        if len(p)<7:await m.answer('/mob_add NAME HP SPEED REWARD BASE_DAMAGE TRAITS');return
        await runtime.db.exec('INSERT INTO mobs(name,hp,speed,reward,base_damage,traits,created_at) VALUES(?,?,?,?,?,?,?)',(p[1],float(p[2]),float(p[3]),int(p[4]),int(p[5]),json.dumps([x for x in p[6].split(',') if x]),now()));await m.answer('✅ Моб создан.')
    @child.message(Command('map_add'))
    @child_only
    async def mapa(m:Message,runtime:Runtime):
        if m.from_user.id!=runtime.owner_id:await m.answer('⛔ Только владелец.');return
        p=(m.text or '').split(maxsplit=4);
        if len(p)!=5:await m.answer('/map_add NAME MAX_WAVES START_HP WAVE_JSON');return
        await manager.limit(runtime,'maps',int(await runtime.db.scalar('SELECT COUNT(*) FROM maps WHERE enabled=1') or 0));json.loads(p[4]);await runtime.db.exec('INSERT INTO maps(name,max_waves,start_hp,waves,created_at) VALUES(?,?,?,?,?)',(p[1],int(p[2]),int(p[3]),p[4],now()));await m.answer('✅ Карта создана.')
    @child.message(Command('banner_add'))
    @child_only
    async def ba(m:Message,runtime:Runtime):
        if m.from_user.id!=runtime.owner_id:await m.answer('⛔ Только владелец.');return
        if not PLANS[runtime.plan].banners:await m.answer('❌ Ротация баннеров доступна с Stars Lite.');return
        p=(m.text or '').split(maxsplit=1);
        if len(p)!=2:await m.answer('/banner_add TEXT');return
        await runtime.db.exec('INSERT INTO banners(text,created_at) VALUES(?,?)',(p[1],now()));await m.answer('✅ Баннер добавлен.')
    @child.message(Command('child_broadcast'))
    @child_only
    async def cbroadcast(m:Message,state:FSMContext,runtime:Runtime):
        if m.from_user.id!=runtime.owner_id:await m.answer('⛔ Только владелец.');return
        await state.set_state(TextState.waiting);await m.answer('Пришли текст рассылки игрокам.')
    @child.message(TextState.waiting)
    @child_only
    async def cbroadcast_text(m:Message,state:FSMContext,runtime:Runtime):
        if m.from_user.id!=runtime.owner_id:return
        await state.clear();rows=await runtime.db.all('SELECT user_id FROM players ORDER BY user_id');cap=None if PLANS[runtime.plan].unlimited_broadcast else 1000;rows=rows[:cap] if cap else rows;sent=0
        for r in rows:
            try:await runtime.bot.send_message(int(r[0]),f'📢 {m.text or ""}');sent+=1
            except TelegramAPIError:pass
        await m.answer(f'✅ Отправлено: {sent}/{len(rows)}')
    @child.message(Command('td_admin'))
    @child_only
    async def ta(m:Message,runtime:Runtime):
        if m.from_user.id!=runtime.owner_id:await m.answer('⛔ Только владелец.');return
        await m.answer('<b>Child Admin</b>\nРедактирование контента, CSV и рассылки.',reply_markup=kb_child())
    @child.callback_query(F.data.startswith('child:'))
    @child_only
    async def cc(c:CallbackQuery,runtime:Runtime):
        if c.from_user.id!=runtime.owner_id:await c.answer('Только владелец',show_alert=True);return
        a=c.data.split(':')[1]
        if a=='start':await manager.start(runtime.bot_id)
        elif a=='stop':await manager.stop(runtime.bot_id)
        elif a=='restart':await manager.restart(runtime.bot_id)
        elif a=='status':await c.message.answer(f'@{runtime.username}\nТариф: {PLANS[runtime.plan].name}\nДо: {expiry(runtime.expires_at)}\nИгроков: {await runtime.db.scalar("SELECT COUNT(*) FROM players")}\nБоёв: {len(runtime.battles)}')
        await c.answer('OK')
    dp.include_router(root);dp.include_router(child);return dp
async def main():
    settings=Settings.load();logging.basicConfig(level=getattr(logging,settings.log_level,logging.INFO),format='%(asctime)s %(levelname)s %(name)s: %(message)s');root=Bot(settings.root_bot_token,default=DefaultBotProperties(parse_mode=ParseMode.HTML));me=await root.get_me();vault=TokenVault(settings.encryption_key);dp=Dispatcher();manager=Manager(dp,settings,vault);dp=await make_dispatcher(manager,int(me.id));manager.dp=dp;await manager.open()
    try:await dp.start_polling(root,handle_signals=False,close_bot_session=False)
    finally:await manager.close();await root.session.close()
if __name__=='__main__':asyncio.run(main())
