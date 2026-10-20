"""Browser sign-in for the staff desk, alongside existing HTTP Basic API access."""
from __future__ import annotations
import base64, hashlib, hmac, html, json, secrets, time, logging
from collections import defaultdict, deque
from urllib.parse import urlencode, urlsplit
from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
import dashboard

COOKIE='faq_desk_session'
LOGIN_COOKIE='faq_desk_login'
MAX_AGE=8*60*60

def safe_next(value: str) -> str:
    try: p=urlsplit(value)
    except ValueError: return '/dashboard'
    if p.scheme or p.netloc or p.path!='/dashboard' or '\\' in value:
        return '/dashboard'
    return value

def same(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode('utf-8'), b.encode('utf-8'))

class DeskAuth:
    def __init__(self,app,logins):
        self.key=secrets.token_bytes(32)
        self.logins=logins
        self.failures=defaultdict(deque)
        self.revoked={}

        @app.middleware('http')
        async def staff_browser(request:Request,call_next):
            path=request.url.path
            protected=path.startswith('/dashboard') or path in ('/login','/logout')
            if protected and request.method=='POST':
                origin=request.headers.get('origin')
                # Embedded browser previews can have an opaque ("null") origin.
                # They still require a signed login challenge or a session CSRF
                # token; never accept an opaque-origin Basic-auth mutation.
                if origin and origin!='null' and urlsplit(origin).netloc!=request.url.netloc:
                    logging.getLogger('faqdesk').warning('Staff form origin rejected: origin=%r host=%r',origin,request.url.netloc)
                    return PlainTextResponse('Please submit from this staff desk.',status_code=403)
                session=self.read(request)
                if origin=='null' and path!='/login' and not session:
                    return PlainTextResponse('Sign in using the staff form first.',status_code=403)
                if session:
                    # Cache the bytes before parsing; the route can read its form too.
                    await request.body()
                    form=await request.form()
                    token=str(form.get('_csrf') or request.headers.get('x-csrf-token') or '')
                    if not same(token,session['csrf']):
                        return PlainTextResponse('Session check failed. Refresh the desk and try again.',status_code=403)
            response=await call_next(request)
            if path=='/dashboard' and request.method=='GET' and response.status_code==401:
                target='/login?'+urlencode({'next':str(request.url.path)+('?' + request.url.query if request.url.query else '')})
                response=RedirectResponse(target,status_code=303)
            if protected:
                response.headers['Cache-Control']='no-store'
                response.headers['Referrer-Policy']='no-referrer'
                response.headers['X-Content-Type-Options']='nosniff'
                response.headers['X-Frame-Options']='DENY'
            return response

        @app.get('/login',response_class=HTMLResponse)
        async def sign_in(request:Request,next:str='/dashboard'):
            target=safe_next(next)
            if self.read(request):
                return RedirectResponse(target,status_code=303)
            challenge=self.pack({'csrf':secrets.token_urlsafe(24),'exp':time.time()+600})
            response=HTMLResponse(dashboard.login_page(target,challenge))
            response.set_cookie(LOGIN_COOKIE,challenge,max_age=600,httponly=True,samesite='strict',secure=request.url.scheme=='https')
            return response

        @app.post('/login')
        async def sign_in_submit(request:Request):
            form=await request.form()
            target=safe_next(str(form.get('next') or '/dashboard'))
            challenge=str(form.get('_csrf') or '')
            if not same(challenge,request.cookies.get(LOGIN_COOKIE,'')) or not self.unpack(challenge):
                return RedirectResponse('/login?'+urlencode({'next':target}),status_code=303)
            peer=request.client.host if request.client else 'local'
            now=time.time()
            recent=self.failures[peer]
            while recent and recent[0]<now-60: recent.popleft()
            if len(recent)>=5:
                return HTMLResponse(dashboard.login_page(target,challenge,'Too many attempts. Wait one minute and try again.'),status_code=429,headers={'Retry-After':'60'})
            username=str(form.get('username') or '')
            password=str(form.get('password') or '')
            config=self.logins().get(username)
            if not config or not password or not same(password,config[0]):
                recent.append(now)
                return HTMLResponse(dashboard.login_page(target,challenge,'The username or password is incorrect.'),status_code=401)
            self.failures.pop(peer,None)
            if len(self.failures)>1000: self.failures.clear()
            session={'user':username,'version':self.version(config[0]),'csrf':secrets.token_urlsafe(24),'exp':now+MAX_AGE}
            response=RedirectResponse(target,status_code=303)
            response.set_cookie(COOKIE,self.pack(session),max_age=MAX_AGE,httponly=True,samesite='strict',secure=request.url.scheme=='https')
            response.delete_cookie(LOGIN_COOKIE)
            return response

        @app.post('/logout')
        async def sign_out(request:Request):
            session=self.read(request)
            if session: self.revoked[session['csrf']]=session['exp']
            self.revoked={k:v for k,v in self.revoked.items() if v>time.time()}
            response=RedirectResponse('/login',status_code=303)
            response.delete_cookie(COOKIE)
            return response

    def version(self,password):
        return hmac.new(self.key,password.encode(),hashlib.sha256).hexdigest()

    def pack(self,data):
        raw=base64.urlsafe_b64encode(json.dumps(data,separators=(',',':')).encode()).decode().rstrip('=')
        sig=hmac.new(self.key,raw.encode(),hashlib.sha256).hexdigest()
        return raw+'.'+sig

    def unpack(self,token):
        try:
            raw,sig=token.rsplit('.',1)
            if not secrets.compare_digest(sig,hmac.new(self.key,raw.encode(),hashlib.sha256).hexdigest()): return None
            data=json.loads(base64.urlsafe_b64decode(raw+'='*(-len(raw)%4)))
            if float(data['exp'])<=time.time(): return None
            return data
        except (ValueError,TypeError,KeyError): return None

    def read(self,request):
        data=self.unpack(request.cookies.get(COOKIE,''))
        if not data or data.get('csrf') in self.revoked: return None
        config=self.logins().get(data.get('user'))
        if not config or not secrets.compare_digest(data.get('version',''),self.version(config[0])): return None
        data['allowed']=config[1]
        return data
