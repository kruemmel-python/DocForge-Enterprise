from __future__ import annotations
import base64, hashlib, hmac, os, secrets, sqlite3, time
from dataclasses import dataclass
from pathlib import Path
@dataclass(slots=True)
class User: username:str; role:str; active:bool
class MyceliaIdentityStore:
    def __init__(self,root:Path):
        self.root=root; root.mkdir(parents=True,exist_ok=True); self.conn=sqlite3.connect(root/"mycelia_identity.sqlite3",check_same_thread=False); self._init()
    def _init(self):
        self.conn.executescript("""create table if not exists users(username text primary key, salt text, password_hash text, role text, active integer, created_at real);
        create table if not exists sessions(token text primary key, username text, csrf text, expires_at real, created_at real);
        create table if not exists audit(id integer primary key, username text, action text, target text, ip text, metadata text, created_at real);"""); self.conn.commit()
    def user_count(self): return self.conn.execute("select count(*) from users").fetchone()[0]
    def _hash(self,pw,salt): return hashlib.pbkdf2_hmac("sha256",pw.encode(),base64.b64decode(salt),200_000).hex()
    def register(self,username,password,role="viewer"):
        if self.user_count()==0: role="admin"
        salt=base64.b64encode(os.urandom(16)).decode(); ph=self._hash(password,salt)
        self.conn.execute("insert into users values(?,?,?,?,?,?)",(username,salt,ph,role,1,time.time())); self.conn.commit(); self.audit(username,"register",role)
    def verify(self,username,password):
        row=self.conn.execute("select * from users where username=? and active=1",(username,)).fetchone()
        return bool(row and hmac.compare_digest(row[2],self._hash(password,row[1])))
    def role(self,username): 
        row=self.conn.execute("select role from users where username=?",(username,)).fetchone(); return row[0] if row else "viewer"
    def create_session(self,username,ttl=28800):
        tok=secrets.token_urlsafe(32); csrf=secrets.token_urlsafe(24); exp=time.time()+ttl
        self.conn.execute("insert into sessions values(?,?,?,?,?)",(tok,username,csrf,exp,time.time())); self.conn.commit(); self.audit(username,"login","webgui"); return tok,csrf
    def session(self,token):
        row=self.conn.execute("select username,csrf,expires_at from sessions where token=?",(token,)).fetchone()
        if not row or row[2]<time.time(): return None
        return {"username":row[0],"csrf":row[1],"role":self.role(row[0])}
    def logout(self,token):
        s=self.session(token); self.conn.execute("delete from sessions where token=?",(token,)); self.conn.commit()
        if s: self.audit(s["username"],"logout","webgui")
    def audit(self,username,action,target="",ip="",metadata=""):
        self.conn.execute("insert into audit(username,action,target,ip,metadata,created_at) values(?,?,?,?,?,?)",(username,action,target,ip,metadata,time.time())); self.conn.commit()
