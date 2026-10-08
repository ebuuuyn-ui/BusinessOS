"""Only permit in-app list/detail return destinations."""
from urllib.parse import urlsplit, unquote
from flask import request

def return_destination(fallback):
    value=request.form.get('return_to','')
    decoded=unquote(value)
    if not value.startswith('/') or decoded.startswith('//') or '\\' in decoded or any(ord(c)<32 for c in decoded):
        return fallback
    parts=urlsplit(value)
    if parts.scheme or parts.netloc or parts.path.startswith(('/static/','/logout','/cikis')):
        return fallback
    return value

def register_navigation(app):
    from flask import g, session
    @app.before_request
    def remember_flash_count():
        g.navigation_flash_count=len(session.get('_flashes',[]))
    @app.after_request
    def return_after_edit(response):
        if request.method=='POST' and (request.endpoint or '').startswith('edit_') and response.status_code in (302,303) and request.form.get('return_to'):
            messages=session.get('_flashes',[])[getattr(g,'navigation_flash_count',0):]
            if any(category=='success' for category,_ in messages) and not any(category in ('error','danger') for category,_ in messages):
                response.headers['Location']=return_destination(response.headers.get('Location','/'))
        return response
