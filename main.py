from nicegui import ui, app
from fastapi import HTTPException
from fastapi.responses import Response
from urllib.parse import quote, urlencode
import sqlite3, os, base64, mimetypes, uuid
from io import BytesIO
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from pathlib import Path
from datetime import date, datetime, timedelta
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError('SUPABASE_URL e SUPABASE_KEY não foram configuradas.')

DB = os.environ.get('ROTAOS_DB', 'rotaos_v10.db')

def con():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


@app.get('/comprovantes/{proof_id}/download')
def download_comprovante(proof_id: int):
    with con() as c:
        proof = c.execute(
            'SELECT nome, mime, dados FROM comprovantes WHERE id=?',
            (proof_id,),
        ).fetchone()
    if not proof:
        raise HTTPException(status_code=404, detail='Comprovante não encontrado')
    filename = proof['nome'] or f'comprovante-{proof_id}'
    mime = proof['mime'] or 'application/octet-stream'
    encoded = quote(filename)
    return Response(
        content=proof['dados'],
        media_type=mime,
        headers={
            'Content-Disposition': f"attachment; filename*=UTF-8''{encoded}",
            'Cache-Control': 'no-store',
        },
    )

def num(v):
    if v in (None, ''): return 0.0
    s = str(v).strip().replace('R$', '').replace(' ', '')
    if ',' in s: s = s.replace('.', '').replace(',', '.')
    try: return float(s)
    except: return 0.0

def money(v):
    return f'R$ {num(v):,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')

def init():
    with con() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS faixas(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tipo TEXT, minimo REAL, maximo REAL NOT NULL, valor REAL
        );
        CREATE TABLE IF NOT EXISTS rotas(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          data TEXT, empresa TEXT, rota_id TEXT, referencia TEXT, destino TEXT,
          fixo REAL,
          pacotes REAL, vp REAL, ep REAL,
          paradas REAL, vs REAL, es REAL,
          km REAL, vk REAL, ek REAL,
          bonus REAL, outro_extra REAL,
          pedagio REAL, outro_reembolso REAL,
          combustivel REAL, combustivel_tratamento TEXT DEFAULT 'Transportadora',
          estacionamento REAL, outro_desconto REAL,
          remuneracao REAL, reembolsos REAL, descontos REAL,
          receber REAL, resultado REAL,
          observacao TEXT, status TEXT DEFAULT 'Previsto',
          criado TEXT
        );
        CREATE TABLE IF NOT EXISTS fechamentos(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          inicio TEXT, fim TEXT, empresa TEXT, previsto REAL,
          informado REAL, diferenca REAL, observacao TEXT, criado TEXT
        );
        CREATE TABLE IF NOT EXISTS comprovantes(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          rota_id_db INTEGER NOT NULL, nome TEXT NOT NULL, mime TEXT, dados BLOB NOT NULL, criado TEXT
        );
        """)
        cols = [r[1] for r in c.execute("PRAGMA table_info(rotas)").fetchall()]
        if 'combustivel_tratamento' not in cols:
            c.execute("ALTER TABLE rotas ADD COLUMN combustivel_tratamento TEXT DEFAULT 'Transportadora'")

def rate(tipo, qtd):
    try:
        return cloud_rate(tipo, qtd)
    except Exception:
        q = num(qtd)
        with con() as c:
            r = c.execute(
                'SELECT valor FROM faixas WHERE tipo=? AND ?>=minimo AND ?<maximo ORDER BY minimo DESC LIMIT 1',
                (tipo, q, q)
            ).fetchone()
        return num(r['valor']) if r else 0

def add_range(tipo, mi, ma, valor):
    if mi in ('', None) or ma in ('', None) or valor in ('', None):
        raise ValueError('Preencha De, Até e R$/un.')
    mi, ma, valor = num(mi), num(ma), num(valor)
    if ma <= mi: raise ValueError('"Até" precisa ser maior que "De".')
    if valor <= 0: raise ValueError('O valor precisa ser maior que zero.')
    with con() as c:
        for r in c.execute('SELECT * FROM faixas WHERE tipo=?', (tipo,)).fetchall():
            if mi < num(r['maximo']) and num(r['minimo']) < ma:
                raise ValueError('Essa faixa cruza outra já cadastrada.')
        c.execute('INSERT INTO faixas(tipo,minimo,maximo,valor) VALUES(?,?,?,?)',
                  (tipo, mi, ma, valor))

init()

def period_rows(search_term='', exact_date='', start_date='', end_date=''):
    with con() as c:
        rows=c.execute('SELECT * FROM rotas ORDER BY data DESC,id DESC').fetchall()
    term=(search_term or '').lower().strip()
    if term:
        rows=[r for r in rows if
              term in (r['rota_id'] or '').lower() or
              term in (r['destino'] or '').lower() or
              term in (r['referencia'] or '').lower() or
              term in (r['data'] or '').lower()]
    if exact_date:
        rows=[r for r in rows if r['data']==exact_date]
    if start_date:
        rows=[r for r in rows if (r['data'] or '')>=start_date]
    if end_date:
        rows=[r for r in rows if (r['data'] or '')<=end_date]
    return rows

def pdf_route_parts(r):
    prod=[]; reimb=[]; disc=[]
    if num(r['fixo']): prod.append('Valor base: '+money(r['fixo']))
    if num(r['pacotes']): prod.append(f"Pacotes: {r['pacotes']:g} x {money(r['vp'])} = {money(r['ep'])}")
    if num(r['paradas']): prod.append(f"Paradas: {r['paradas']:g} x {money(r['vs'])} = {money(r['es'])}")
    if num(r['km']): prod.append(f"KM: {r['km']:g} km - valor da faixa: {money(r['ek'])}")
    if num(r['bonus']): prod.append('Bonus: '+money(r['bonus']))
    if num(r['outro_extra']): prod.append('Outro adicional: '+money(r['outro_extra']))
    if num(r['pedagio']): reimb.append('Pedagio: '+money(r['pedagio']))
    if num(r['outro_reembolso']): reimb.append('Outro reembolso: '+money(r['outro_reembolso']))
    if r['combustivel_tratamento']=='Reembolsável' and num(r['combustivel']):
        reimb.append('Combustivel reembolsavel: '+money(r['combustivel']))
    if r['combustivel_tratamento']=='Descontado no pagamento' and num(r['combustivel']):
        disc.append('Combustivel descontado: '+money(r['combustivel']))
    if num(r['estacionamento']): disc.append('Estacionamento: '+money(r['estacionamento']))
    if num(r['outro_desconto']): disc.append('Outro desconto: '+money(r['outro_desconto']))
    return prod,reimb,disc

@app.get('/historico/pdf')
def historico_pdf(search: str='', date_filter: str='', start: str='', end: str=''):
    rows=period_rows(search,date_filter,start,end)
    if not rows:
        raise HTTPException(status_code=404, detail='Nenhuma rota encontrada para este filtro')
    buf=BytesIO()
    doc=SimpleDocTemplate(buf,pagesize=A4,rightMargin=14*mm,leftMargin=14*mm,topMargin=14*mm,bottomMargin=14*mm)
    styles=getSampleStyleSheet()
    title=ParagraphStyle('rt',parent=styles['Title'],fontName='Helvetica-Bold',fontSize=18,leading=22,alignment=TA_CENTER)
    body=ParagraphStyle('rb',parent=styles['BodyText'],fontSize=9,leading=12)
    small=ParagraphStyle('rs',parent=styles['BodyText'],fontSize=8,leading=10)
    story=[Paragraph('RotaOS - Fechamento de rotas',title),Spacer(1,4*mm)]
    periodo = f"Periodo: {start or date_filter or 'inicio'} a {end or date_filter or 'fim'}" if (start or end or date_filter) else 'Periodo: todas as rotas filtradas'
    story += [Paragraph(periodo,body),Paragraph(f"Quantidade de rotas: {len(rows)}",body),Spacer(1,5*mm)]
    tr=tre=td=ta=0.0
    for i,r in enumerate(rows,1):
        tr+=num(r['remuneracao']); tre+=num(r['reembolsos']); td+=num(r['descontos']); ta+=num(r['receber'])
        prod,reimb,disc=pdf_route_parts(r)
        story.append(Paragraph(f"<b>{i}. Rota {r['rota_id'] or '-'}</b>",body))
        meta=[r['data'] or '-']
        if r['referencia']: meta.append('Rota: '+r['referencia'])
        if r['destino']: meta.append('Destino/regiao: '+r['destino'])
        story.append(Paragraph(' | '.join(meta),small))
        cells=[]
        if prod: cells.append([Paragraph('<b>REMUNERACAO</b>',small),Paragraph('<br/>'.join(prod)+f"<br/><b>Total: {money(r['remuneracao'])}</b>",small)])
        if reimb: cells.append([Paragraph('<b>REEMBOLSOS</b>',small),Paragraph('<br/>'.join(reimb)+f"<br/><b>Total: {money(r['reembolsos'])}</b>",small)])
        if disc: cells.append([Paragraph('<b>DESCONTOS</b>',small),Paragraph('<br/>'.join(disc)+f"<br/><b>Total: {money(r['descontos'])}</b>",small)])
        if cells:
            t=Table(cells,colWidths=[38*mm,137*mm])
            t.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('BACKGROUND',(0,0),(0,-1),colors.HexColor('#F2F6FA')),('BOX',(0,0),(-1,-1),.4,colors.HexColor('#C9D3DD')),('INNERGRID',(0,0),(-1,-1),.25,colors.HexColor('#D9E1E8')),('PADDING',(0,0),(-1,-1),5)]))
            story += [Spacer(1,2*mm),t]
        story += [Spacer(1,2*mm),Paragraph(f"<b>A receber: {money(r['receber'])}</b>",body)]
        if r['observacao']: story.append(Paragraph('Observacao: '+r['observacao'],small))
        story.append(Spacer(1,5*mm))
    summary=Table([
        ['RESUMO DO PERIODO',''],
        ['Remuneracao',money(tr)],['Reembolsos',money(tre)],['Descontos',money(td)],['TOTAL A RECEBER',money(ta)]
    ],colWidths=[95*mm,80*mm])
    summary.setStyle(TableStyle([('SPAN',(0,0),(1,0)),('BACKGROUND',(0,0),(1,0),colors.HexColor('#DCEEFF')),('FONTNAME',(0,0),(1,0),'Helvetica-Bold'),('FONTNAME',(0,4),(1,4),'Helvetica-Bold'),('ALIGN',(1,1),(1,-1),'RIGHT'),('BOX',(0,0),(-1,-1),.6,colors.HexColor('#7D9AB5')),('INNERGRID',(0,1),(-1,-1),.3,colors.HexColor('#CCD7E0')),('PADDING',(0,0),(-1,-1),7)]))
    story.append(summary)
    doc.build(story)
    data=buf.getvalue(); buf.close()
    filename=f"RotaOS_fechamento_{start or date_filter or 'inicio'}_{end or date_filter or 'fim'}.pdf"
    return Response(content=data,media_type='application/pdf',headers={'Content-Disposition':f"attachment; filename*=UTF-8''{quote(filename)}",'Cache-Control':'no-store'})

def _new_supabase_client():
    return create_client(SUPABASE_URL, SUPABASE_KEY)

def _cloud_session():
    user_id = app.storage.user.get('user_id')
    access = app.storage.user.get('access_token')
    refresh = app.storage.user.get('refresh_token')
    if not user_id or not access or not refresh:
        raise RuntimeError('Sessão do usuário não encontrada.')
    client = _new_supabase_client()
    client.auth.set_session(access, refresh)
    return client, user_id

def cloud_rate(tipo, qtd):
    q = num(qtd)
    client, user_id = _cloud_session()
    res = (client.table('faixas').select('valor,minimo,maximo')
           .eq('user_id', user_id).eq('tipo', tipo)
           .lte('minimo', q).gt('maximo', q)
           .order('minimo', desc=True).limit(1).execute())
    return num(res.data[0]['valor']) if res.data else 0

def cloud_ranges(tipo):
    client, user_id = _cloud_session()
    res = (client.table('faixas').select('*').eq('user_id', user_id)
           .eq('tipo', tipo).order('minimo').execute())
    return res.data or []

def add_cloud_range(tipo, mi, ma, valor):
    if mi in ('', None) or ma in ('', None) or valor in ('', None):
        raise ValueError('Preencha De, Até e R$/un.')
    mi, ma, valor = num(mi), num(ma), num(valor)
    if ma <= mi: raise ValueError('"Até" precisa ser maior que "De".')
    if valor <= 0: raise ValueError('O valor precisa ser maior que zero.')
    client, user_id = _cloud_session()
    rows = cloud_ranges(tipo)
    for r in rows:
        if mi < num(r['maximo']) and num(r['minimo']) < ma:
            raise ValueError('Essa faixa cruza outra já cadastrada.')
    client.table('faixas').insert({'user_id':user_id,'tipo':tipo,'minimo':mi,'maximo':ma,'valor':valor}).execute()

def delete_cloud_range(range_id):
    client, user_id = _cloud_session()
    client.table('faixas').delete().eq('id', range_id).eq('user_id', user_id).execute()

def _safe_storage_name(name):
    base = os.path.basename(name or 'comprovante')
    return ''.join(ch if ch.isalnum() or ch in '._-' else '_' for ch in base)

def upload_cloud_proof(client, user_id, cloud_route_id, proof):
    safe = _safe_storage_name(proof['nome'])
    path = f"{user_id}/{cloud_route_id}/{uuid.uuid4().hex}_{safe}"
    opts = {'content-type': proof.get('mime') or 'application/octet-stream', 'upsert': 'false'}
    client.storage.from_('comprovantes').upload(path, proof['dados'], opts)
    client.table('comprovantes').insert({
        'user_id': user_id, 'rota_id': cloud_route_id,
        'arquivo_path': path, 'nome_arquivo': proof['nome'],
    }).execute()
    return path

def cloud_proofs(cloud_route_id):
    client, user_id = _cloud_session()
    res = (client.table('comprovantes').select('*').eq('user_id', user_id)
           .eq('rota_id', cloud_route_id).order('id', desc=True).execute())
    return client, user_id, (res.data or [])

def cloud_proof_bytes(client, path):
    return client.storage.from_('comprovantes').download(path)


def cloud_route_to_local(r):
    """Adapta uma linha do Supabase ao formato já usado pela interface/PDF local."""
    pac = num(r.get('pacotes')); par = num(r.get('paradas'))
    ep = num(r.get('valor_pacotes')); es = num(r.get('valor_paradas')); ek = num(r.get('valor_km'))
    bonus = num(r.get('bonus'))
    outro_extra = num(r.get('outro_adicional'))
    # Compatibilidade com registros V2.2/V2.3, anteriores às colunas detalhadas.
    if not bonus and not outro_extra:
        outro_extra = num(r.get('outros_adicionais'))
    combustivel = num(r.get('combustivel_valor'))
    tratamento = r.get('combustivel_tratamento') or ''
    if not tratamento:
        if num(r.get('combustivel_desconto')):
            tratamento = 'Descontado no pagamento'; combustivel = num(r.get('combustivel_desconto'))
        else:
            tratamento = 'Transportadora'
    outro_reembolso = num(r.get('outro_reembolso'))
    pedagio = num(r.get('pedagio_reembolso'))
    reemb = pedagio + outro_reembolso + (combustivel if tratamento == 'Reembolsável' else 0)
    descontos = (combustivel if tratamento == 'Descontado no pagamento' else 0) + num(r.get('estacionamento_desconto')) + num(r.get('outros_descontos'))
    remuneracao = num(r.get('valor_base')) + ep + es + ek + bonus + outro_extra
    receber = remuneracao + reemb - descontos
    local_id = None
    try:
        with con() as c:
            hit = c.execute('SELECT id FROM rotas WHERE data=? AND rota_id=? ORDER BY id DESC LIMIT 1',
                            (str(r.get('data') or ''), str(r.get('rota_id') or ''))).fetchone()
            if hit: local_id = hit['id']
    except Exception:
        pass
    return {
        'id': local_id, 'cloud_id': r.get('id'), 'data': str(r.get('data') or ''), 'empresa': '',
        'rota_id': str(r.get('rota_id') or ''), 'referencia': str(r.get('rota') or ''), 'destino': str(r.get('destino') or ''),
        'fixo': num(r.get('valor_base')), 'pacotes': pac, 'vp': (ep/pac if pac else 0), 'ep': ep,
        'paradas': par, 'vs': (es/par if par else 0), 'es': es, 'km': num(r.get('km')), 'vk': 0, 'ek': ek,
        'bonus': bonus, 'outro_extra': outro_extra, 'pedagio': pedagio, 'outro_reembolso': outro_reembolso,
        'combustivel': combustivel, 'combustivel_tratamento': tratamento,
        'estacionamento': num(r.get('estacionamento_desconto')), 'outro_desconto': num(r.get('outros_descontos')),
        'remuneracao': remuneracao, 'reembolsos': reemb, 'descontos': descontos, 'receber': receber, 'resultado': receber,
        'observacao': str(r.get('observacao') or ''), 'status': 'Previsto', 'criado': str(r.get('created_at') or ''),
    }


def _save_session(login):
    if not login or not login.session or not login.user:
        return False
    app.storage.user['access_token'] = login.session.access_token
    app.storage.user['refresh_token'] = login.session.refresh_token
    app.storage.user['user_id'] = login.user.id
    app.storage.user['email'] = login.user.email or ''
    return True


def _restore_session():
    access = app.storage.user.get('access_token')
    refresh = app.storage.user.get('refresh_token')
    if not access or not refresh:
        return None
    client = _new_supabase_client()
    try:
        client.auth.set_session(access, refresh)
        user = client.auth.get_user()
        if user and user.user:
            app.storage.user['user_id'] = user.user.id
            app.storage.user['email'] = user.user.email or ''
            return client
    except Exception:
        app.storage.user.clear()
    return None


def render_login():
    ui.add_head_html("""<style>body{background:#f5f7fb}.login-shell{min-height:92vh;display:flex;align-items:center;justify-content:center;padding:18px}.login-card{width:100%;max-width:440px;border-radius:22px;padding:28px}.login-brand{font-size:28px;font-weight:900}.login-muted{color:#667085}</style>""")
    with ui.element('div').classes('login-shell w-full'):
        with ui.card().classes('login-card shadow-lg'):
            ui.label('🚚 RotaOS').classes('login-brand')
            ui.label('O sistema operacional de quem vive de rota.').classes('login-muted mb-4')
            with ui.tabs().classes('w-full') as auth_tabs:
                entrar_tab = ui.tab('Entrar')
                criar_tab = ui.tab('Criar conta')
            with ui.tab_panels(auth_tabs, value=entrar_tab).classes('w-full bg-transparent'):
                with ui.tab_panel(entrar_tab):
                    email = ui.input('E-mail').props('outlined type=email autocomplete=email').classes('w-full')
                    senha = ui.input('Senha').props('outlined type=password autocomplete=current-password').classes('w-full mt-3')
                    async def entrar():
                        if not email.value or not senha.value:
                            ui.notify('Informe e-mail e senha.', type='warning'); return
                        try:
                            client = _new_supabase_client()
                            login = client.auth.sign_in_with_password({'email': email.value.strip(), 'password': senha.value})
                            if not _save_session(login):
                                raise RuntimeError('O Supabase não retornou uma sessão válida.')
                            ui.navigate.to('/')
                        except Exception:
                            ui.notify('Não foi possível entrar. Confira e-mail e senha.', type='negative')
                    ui.button('ENTRAR', icon='login', on_click=entrar).classes('w-full mt-5').props('unelevated no-caps')
                with ui.tab_panel(criar_tab):
                    nome = ui.input('Nome').props('outlined autocomplete=name').classes('w-full')
                    novo_email = ui.input('E-mail').props('outlined type=email autocomplete=email').classes('w-full mt-3')
                    nova_senha = ui.input('Senha').props('outlined type=password autocomplete=new-password').classes('w-full mt-3')
                    confirma = ui.input('Confirmar senha').props('outlined type=password autocomplete=new-password').classes('w-full mt-3')
                    async def criar_conta():
                        if not nome.value or not novo_email.value or not nova_senha.value:
                            ui.notify('Preencha nome, e-mail e senha.', type='warning'); return
                        if nova_senha.value != confirma.value:
                            ui.notify('As senhas não conferem.', type='warning'); return
                        if len(nova_senha.value) < 6:
                            ui.notify('Use uma senha com pelo menos 6 caracteres.', type='warning'); return
                        try:
                            client = _new_supabase_client()
                            cadastro = client.auth.sign_up({'email': novo_email.value.strip(), 'password': nova_senha.value, 'options': {'data': {'nome': nome.value.strip()}}})
                            if cadastro.session and cadastro.user:
                                _save_session(cadastro)
                                ui.navigate.to('/')
                            else:
                                auth_tabs.value = entrar_tab
                                ui.notify('Conta criada. Entre com seu e-mail e senha.', type='positive')
                        except Exception:
                            ui.notify('Não foi possível criar a conta. Verifique os dados ou tente outro e-mail.', type='negative')
                    ui.button('CRIAR CONTA', icon='person_add', on_click=criar_conta).classes('w-full mt-5').props('unelevated no-caps')


def render_rotaos():
    ui.add_head_html("""
    <style>
    body{background:#f5f6f8}.wrap{max-width:1420px;margin:auto}.card{border-radius:16px}
    .blue{border:1.5px solid #1976d2}.muted{color:#667085}.title{font-size:19px;font-weight:800}
    .twocol{display:grid;grid-template-columns:1fr 1fr;gap:22px}
    .threecol{display:grid;grid-template-columns:repeat(3,1fr);gap:18px}
    .summary{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
    .metric{background:#f8fafc;border:1px solid #e7edf5;border-radius:14px;padding:14px;white-space:pre-line}
    .summary-shell{background:linear-gradient(135deg,#f7fbff 0%,#ffffff 60%);border:1.5px solid #1976d2;border-radius:18px;padding:18px}
    .receive-hero{margin-top:12px;background:linear-gradient(135deg,#1565c0,#1976d2);border-radius:16px;padding:18px;color:white}
    .receive-label{font-size:12px;font-weight:800;letter-spacing:.08em;opacity:.88}
    .receive-value{font-size:30px;font-weight:900;line-height:1.15;margin-top:4px}
    .close-chip{min-width:34px!important;width:34px!important;height:34px!important;border-radius:50%!important}
    .dynamic-row{background:#fff;border:1px solid #e5e7eb;border-radius:14px;padding:9px}

    .history-row:hover{background:#f0f6ff}.section{min-height:240px}.mobile-actions{display:flex;gap:8px;flex-wrap:wrap}
    @media(max-width:900px){.wrap{width:100%;padding:0 8px}.twocol,.threecol{grid-template-columns:1fr}.summary{grid-template-columns:1fr 1fr 1fr}.section{min-height:auto}.q-header{padding-left:12px!important;padding-right:12px!important}.q-tab{padding:0 10px}.q-tab__label{font-size:12px}.mobile-stack{flex-direction:column!important;align-items:stretch!important}.mobile-stack>*{width:100%!important;max-width:none!important}.metric{padding:10px;font-size:12px}.receive-value{font-size:28px}.summary-shell{padding:13px}.history-card-row{flex-direction:column!important;align-items:flex-start!important;gap:8px!important}.history-money{width:auto!important}.dialog-mobile{width:96vw!important;max-width:820px!important;max-height:92vh!important;overflow:auto!important}}
    </style>
    """)
    ui.add_head_html("""
    <script>
    document.addEventListener('focusin', (e) => {
      const el = e.target;
      if (!el || el.tagName !== 'INPUT') return;
      const v = (el.value || '').trim();
      if (v === '0' || v === '0,00' || v === '0.00') {
        setTimeout(() => el.select(), 0);
      }
    });
    </script>
    """)

    with ui.header().classes('px-8 justify-between'):
        ui.label('🚚 RotaOS V2.6 ONLINE').classes('text-xl font-bold')
        with ui.row().classes('items-center gap-2'):
            ui.label('Produção • Recebimentos • Conferência').classes('text-sm')

            def _logout():
                try:
                    client = _new_supabase_client()
                    access = app.storage.user.get('access_token')
                    refresh = app.storage.user.get('refresh_token')
                    if access and refresh:
                        client.auth.set_session(access, refresh)
                        client.auth.sign_out()
                except Exception:
                    pass
                app.storage.user.clear()
                ui.navigate.to('/')

            def _profile_dialog():
                client, uid = _cloud_session()
                try:
                    u = client.auth.get_user().user
                    meta = u.user_metadata or {}
                except Exception:
                    meta = {}
                with ui.dialog() as dlg, ui.card().classes('dialog-mobile w-[520px] p-5'):
                    ui.label('Perfil').classes('text-xl font-bold')
                    avatar_box = ui.column().classes('w-full items-center')
                    def render_avatar():
                        avatar_box.clear()
                        path = (meta or {}).get('avatar_path')
                        with avatar_box:
                            if path:
                                try:
                                    signed = client.storage.from_('avatars').create_signed_url(path, 3600)
                                    url = signed.get('signedURL') or signed.get('signedUrl') or signed.get('signed_url')
                                    if url:
                                        ui.image(url).classes('w-24 h-24 rounded-full object-cover')
                                    else:
                                        ui.icon('account_circle').classes('text-7xl text-grey-6')
                                except Exception:
                                    ui.icon('account_circle').classes('text-7xl text-grey-6')
                            else:
                                ui.icon('account_circle').classes('text-7xl text-grey-6')
                    render_avatar()
                    nome = ui.input('Nome', value=(meta or {}).get('nome','')).props('outlined').classes('w-full')
                    ui.input('E-mail', value=app.storage.user.get('email','')).props('outlined readonly').classes('w-full')
                    async def avatar_upload(e):
                        try:
                            raw = await e.file.read()
                            if len(raw) > 5_000_000:
                                ui.notify('Foto maior que 5 MB.', type='warning'); return
                            mime = e.file.content_type or ''
                            if not mime.startswith('image/'):
                                ui.notify('Envie uma imagem.', type='warning'); return
                            ext = Path(e.file.name or 'avatar.jpg').suffix.lower() or '.jpg'
                            path = f'{uid}/avatar{ext}'
                            try:
                                client.storage.from_('avatars').remove([path])
                            except Exception:
                                pass
                            client.storage.from_('avatars').upload(path, raw, {'content-type': mime, 'upsert':'true'})
                            meta['avatar_path'] = path
                            client.auth.update_user({'data': {'nome': nome.value.strip(), 'avatar_path': path}})
                            render_avatar(); ui.notify('Foto atualizada.', type='positive')
                        except Exception as ex:
                            ui.notify(f'Não foi possível atualizar a foto: {ex}', type='negative')
                    ui.upload(label='ALTERAR FOTO', on_upload=avatar_upload, auto_upload=True, max_file_size=5_000_000).props('accept="image/*" flat color=primary').classes('w-full')
                    def save_profile():
                        try:
                            client.auth.update_user({'data': {'nome': nome.value.strip(), 'avatar_path': meta.get('avatar_path')}})
                            ui.notify('Perfil atualizado.', type='positive')
                        except Exception as ex:
                            ui.notify(f'Não foi possível atualizar o perfil: {ex}', type='negative')
                    with ui.row().classes('w-full justify-end'):
                        ui.button('Salvar', on_click=save_profile).props('no-caps')
                        ui.button('Fechar', on_click=dlg.close).props('flat no-caps')
                dlg.open()

            def _settings_dialog():
                with ui.dialog() as dlg, ui.card().classes('dialog-mobile w-[520px] p-5'):
                    ui.label('Configurações').classes('text-xl font-bold')
                    ui.label('Alterar senha').classes('font-bold mt-2')
                    p1 = ui.input('Nova senha').props('outlined type=password autocomplete=new-password').classes('w-full')
                    p2 = ui.input('Confirmar nova senha').props('outlined type=password autocomplete=new-password').classes('w-full')
                    def change_password():
                        if len(p1.value or '') < 6:
                            ui.notify('Use uma senha com pelo menos 6 caracteres.', type='warning'); return
                        if p1.value != p2.value:
                            ui.notify('As senhas não conferem.', type='warning'); return
                        try:
                            client, _ = _cloud_session()
                            client.auth.update_user({'password': p1.value})
                            p1.value=''; p2.value=''
                            ui.notify('Senha alterada com sucesso.', type='positive')
                        except Exception as ex:
                            ui.notify(f'Não foi possível alterar a senha: {ex}', type='negative')
                    with ui.row().classes('w-full justify-end'):
                        ui.button('Alterar senha', on_click=change_password).props('no-caps')
                        ui.button('Fechar', on_click=dlg.close).props('flat no-caps')
                dlg.open()

            with ui.button(icon='menu').props('round flat color=white'):
                with ui.menu():
                    ui.menu_item('Perfil', on_click=_profile_dialog)
                    ui.menu_item('Configurações', on_click=_settings_dialog)
                    ui.separator()
                    ui.menu_item('Sair', on_click=_logout)

    with ui.tabs().classes('w-full') as tabs:
        reg = ui.tab('Registrar', icon='add_circle')
        close = ui.tab('Fechamento', icon='calculate')
        hist = ui.tab('Histórico', icon='history')
        cfg = ui.tab('Faixas', icon='tune')

    with ui.tab_panels(tabs, value=reg).classes('w-full wrap bg-transparent'):

        # ---------- REGISTRAR ----------
        with ui.tab_panel(reg):
            ui.label('Registrar rota').classes('text-2xl font-bold')
            ui.label('Registre o que foi produzido, o que será reembolsado e o que será descontado.').classes('muted mb-4')

            fields = {}
            state = {}

            with ui.card().classes('w-full card p-5'):
                with ui.row().classes('w-full gap-4 items-end mobile-stack'):
                    data_i = ui.input('Data', value=date.today().isoformat()).props('type=date outlined').classes('w-44')
                    empresa_i = type('_HiddenValue', (), {'value': ''})()
                    rotaid_i = ui.input('ID da rota').props('outlined').classes('w-48')
                    ref_i = ui.input('Rota', placeholder='Ex.: R8 AM').props('outlined').classes('w-48')
                    destino_i = ui.input('Destino / região').props('outlined').classes('grow')
                    fixo_i = ui.input('Valor base (R$)', value='0,00').props('outlined inputmode=decimal').classes('w-44')

            with ui.element('div').classes('threecol w-full mt-5'):
                with ui.card().classes('card p-5 w-full section'):
                    ui.label('➕ Adicionais').classes('title')
                    ui.label('Valores que remuneram o trabalho da rota.').classes('muted mb-3')
                    with ui.row().classes('gap-2'):
                        bp = ui.button('+ Pacotes').props('outline no-caps')
                        bs = ui.button('+ Paradas').props('outline no-caps')
                        bk = ui.button('+ KM').props('outline no-caps')
                        boe = ui.button('+ Outro').props('outline no-caps')
                    prod_box = ui.column().classes('w-full gap-2 mt-3')

                with ui.card().classes('card p-5 w-full section'):
                    ui.label('↩️ Reembolsos').classes('title')
                    ui.label('Você paga agora e a empresa devolve depois.').classes('muted mb-3')
                    with ui.row().classes('gap-2'):
                        bped = ui.button('+ Pedágio').props('outline no-caps')
                        bore = ui.button('+ Outro').props('outline no-caps')
                    reimb_box = ui.column().classes('w-full gap-2 mt-3')

                with ui.card().classes('card p-5 w-full section'):
                    ui.label('➖ Descontos / custos').classes('title')
                    ui.label('Só reduzem o pagamento quando realmente são descontados do motorista.').classes('muted mb-3')
                    with ui.row().classes('gap-2'):
                        bcomb = ui.button('+ Combustível').props('outline no-caps')
                        best = ui.button('+ Estacionamento').props('outline no-caps')
                        bod = ui.button('+ Outro').props('outline no-caps')
                    disc_box = ui.column().classes('w-full gap-2 mt-3')

            with ui.card().classes('w-full card p-5 mt-5'):
                ui.label('📝 OBSERVAÇÕES DA ROTA').classes('title')
                ui.label('Registre aqui ocorrências ou informações importantes desta rota. Campo opcional.').classes('muted text-sm mb-2')
                obs = ui.textarea('Observações', placeholder='Ex.: atraso na coleta, endereço divergente, ocorrência com pacote...').props('outlined autogrow').classes('w-full')

            # Comprovantes escolhidos antes de salvar ficam apenas em memória.
            # Só são gravados e vinculados depois que a rota recebe um ID no banco.
            pending_proofs = []
            with ui.card().classes('w-full card p-5 mt-4'):
                ui.label('📎 COMPROVANTES DA ROTA').classes('title')
                ui.label(
                    'Guarde prints, fotos ou documentos relacionados a esta rota. '
                    'Eles podem ajudar na conferência de pagamentos e na comprovação do serviço realizado.'
                ).classes('muted text-sm')
                pending_box = ui.column().classes('w-full gap-2 mt-3')

                def render_pending():
                    pending_box.clear()
                    with pending_box:
                        if not pending_proofs:
                            ui.label('Nenhum comprovante selecionado.').classes('muted')
                        for i, item in enumerate(pending_proofs):
                            with ui.row().classes('w-full items-center border rounded-lg p-2 gap-3'):
                                ui.icon('image' if item['mime'].startswith('image/') else 'description')
                                ui.label(item['nome']).classes('grow')
                                ui.label(f"{len(item['dados'])/1024:.1f} KB").classes('muted text-xs')
                                def remove_pending(idx=i):
                                    if 0 <= idx < len(pending_proofs):
                                        pending_proofs.pop(idx)
                                    render_pending()
                                ui.button(icon='delete', on_click=remove_pending).props('flat dense color=negative')

                async def stage_proof(e):
                    try:
                        raw = await e.file.read()
                        mime = getattr(e.file, 'content_type', None) or ''
                        name = getattr(e.file, 'name', None) or 'comprovante'
                        if len(raw) > 8 * 1024 * 1024:
                            ui.notify('Arquivo maior que 8 MB.', type='warning')
                            return
                        if not (mime.startswith('image/') or mime == 'application/pdf'):
                            ui.notify('Envie uma imagem ou PDF.', type='warning')
                            return
                        pending_proofs.append({'nome': name, 'mime': mime, 'dados': raw})
                        render_pending()
                        ui.notify('Comprovante pronto para ser salvo com a rota.', type='positive')
                    except Exception as ex:
                        ui.notify(f'Não foi possível preparar o comprovante: {ex}', type='negative')

                render_pending()
                upload_slot = ui.column().classes('w-full')
                def render_uploader():
                    upload_slot.clear()
                    with upload_slot:
                        ui.upload(
                            label='ANEXAR COMPROVANTE',
                            on_upload=stage_proof,
                            auto_upload=True,
                            max_file_size=8_000_000,
                        ).props('accept="image/*,.pdf" flat color=primary').classes('mt-2')
                render_uploader()

            with ui.element('div').classes('summary-shell w-full mt-5'):
                ui.label('RESUMO DA ROTA').classes('text-sm font-bold muted')
                with ui.element('div').classes('summary w-full mt-2'):
                    lrem = ui.label('REMUNERAÇÃO\nR$ 0,00').classes('metric font-bold')
                    lrei = ui.label('REEMBOLSOS\nR$ 0,00').classes('metric font-bold')
                    ldes = ui.label('DESCONTOS\nR$ 0,00').classes('metric font-bold')
                with ui.element('div').classes('receive-hero w-full'):
                    ui.label('PREVISÃO A RECEBER').classes('receive-label')
                    lrec = ui.label('R$ 0,00').classes('receive-value')
                save_btn = ui.button('SALVAR ROTA', icon='save').props('no-caps unelevated').classes('w-full mt-3 h-12')

            def recalc():
                f = num(fixo_i.value)
                pq = num(fields['pq'].value) if 'pq' in fields else 0
                sq = num(fields['sq'].value) if 'sq' in fields else 0
                kq = num(fields['kq'].value) if 'kq' in fields else 0
                vp, vs, vk = rate('pacotes', pq), rate('paradas', sq), rate('km', kq)
                ep, es, ek = pq*vp, sq*vs, (vk if kq > 0 else 0)
                for k, val, rv in [('p',ep,vp),('s',es,vs),('k',ek,vk)]:
                    if k+'out' in fields: fields[k+'out'].text = money(val)
                    if k+'rate' in fields:
                        if not rv:
                            fields[k+'rate'].text = 'sem faixa'
                        elif k == 'k':
                            fields[k+'rate'].text = 'faixa: ' + money(rv)
                        else:
                            fields[k+'rate'].text = money(rv) + '/un.'
                bonus = num(fields['bonus'].value) if 'bonus' in fields else 0
                oe = num(fields['oe'].value) if 'oe' in fields else 0
                ped = num(fields['ped'].value) if 'ped' in fields else 0
                ore = num(fields['ore'].value) if 'ore' in fields else 0
                comb = num(fields['comb'].value) if 'comb' in fields else 0
                comb_mode = fields['comb_mode'].value if 'comb_mode' in fields else 'Pago pela transportadora'
                est = num(fields['est'].value) if 'est' in fields else 0
                od = num(fields['od'].value) if 'od' in fields else 0

                remuneracao = f+ep+es+ek+bonus+oe
                reemb_comb = comb if comb_mode == 'Reembolsável' else 0
                desconto_comb = comb if comb_mode == 'Descontado no pagamento' else 0
                reembolsos = ped+ore+reemb_comb
                descontos = desconto_comb+est+od
                receber = remuneracao+reembolsos-descontos
                # Resultado pessoal: somente custos realmente bancados pelo motorista reduzem a remuneração.
                resultado = receber

                lrem.text='REMUNERAÇÃO\n'+money(remuneracao)
                lrei.text='REEMBOLSOS\n'+money(reembolsos)
                ldes.text='DESCONTOS\n'+money(descontos)
                lrec.text=money(receber)

                state.clear()
                state.update(
                    data=data_i.value, empresa=empresa_i.value or '', rota_id=rotaid_i.value or '',
                    referencia=ref_i.value or '', destino=destino_i.value or '', fixo=f,
                    pacotes=pq, vp=vp, ep=ep, paradas=sq, vs=vs, es=es, km=kq, vk=vk, ek=ek,
                    bonus=bonus, outro_extra=oe, pedagio=ped, outro_reembolso=ore,
                    combustivel=comb, combustivel_tratamento=comb_mode, estacionamento=est, outro_desconto=od,
                    remuneracao=remuneracao, reembolsos=reembolsos, descontos=descontos,
                    receber=receber, resultado=resultado, observacao=obs.value or ''
                )

            def remove_dynamic(keys, element):
                for key in keys:
                    fields.pop(key, None)
                element.delete()
                recalc()

            def unit(k, title, box):
                if k+'q' in fields: return
                with box:
                    row = ui.row().classes('dynamic-row w-full items-center gap-2')
                    with row:
                        ui.label(title).classes('w-24 font-bold')
                        q = ui.input('Qtd.', value='0').props('outlined dense inputmode=numeric').classes('w-24')
                        rt = ui.label('sem faixa').classes('w-28 muted')
                        out = ui.label('R$ 0,00').classes('grow text-right font-bold')
                        ui.button(icon='close', on_click=lambda: remove_dynamic(
                            [k+'q', k+'rate', k+'out'], row
                        )).props('flat round dense color=grey-7').classes('close-chip')
                fields[k+'q']=q; fields[k+'rate']=rt; fields[k+'out']=out
                q.on('input', lambda e: recalc()); recalc()

            def cash(k, title, box):
                if k in fields: return
                with box:
                    row = ui.row().classes('dynamic-row w-full items-center gap-2')
                    with row:
                        ui.label(title).classes('w-40 font-bold')
                        x = ui.input('Valor R$', value='0,00').props('outlined dense inputmode=decimal').classes('grow')
                        ui.button(icon='close', on_click=lambda: remove_dynamic(
                            [k], row
                        )).props('flat round dense color=grey-7').classes('close-chip')
                fields[k]=x; x.on('input', lambda e: recalc()); recalc()

            bp.on('click', lambda: unit('p','📦 Pacotes',prod_box))
            bs.on('click', lambda: unit('s','📍 Paradas',prod_box))
            bk.on('click', lambda: unit('k','🚗 KM',prod_box))
            boe.on('click', lambda: cash('oe','Outro adicional',prod_box))
            bped.on('click', lambda: cash('ped','Pedágio reembolsável',reimb_box))
            bore.on('click', lambda: cash('ore','Outro reembolso',reimb_box))
            def fuel():
                if 'comb' in fields: return
                with disc_box:
                    fuel_card = ui.column().classes('w-full dynamic-row gap-2')
                    with fuel_card:
                        with ui.row().classes('w-full items-center'):
                            ui.label('⛽ Combustível').classes('font-bold grow')
                            ui.button(icon='close', on_click=lambda: remove_dynamic(
                                ['comb','comb_mode'], fuel_card
                            )).props('flat round dense color=grey-7').classes('close-chip')
                        with ui.row().classes('w-full items-end gap-3 mobile-stack'):
                            x = ui.input('Valor R$', value='0,00').props('outlined dense inputmode=decimal').classes('w-40')
                            mode = ui.select(
                                ['Pago pela transportadora', 'Descontado no pagamento', 'Reembolsável'],
                                value='Pago pela transportadora',
                                label='Tratamento financeiro',
                            ).props('outlined dense').classes('grow')
                        hint = ui.label('Não altera o valor a receber.').classes('muted text-sm')
                        def changed():
                            m = mode.value
                            hint.text = {
                                'Pago pela transportadora':'Não altera o valor a receber.',
                                'Descontado no pagamento':'Será subtraído da previsão de pagamento.',
                                'Reembolsável':'Será somado ao valor que a transportadora deve pagar.',
                            }[m]
                            recalc()
                        x.on('input', lambda e: changed())
                        mode.on('update:model-value', lambda e: changed())
                fields['comb']=x; fields['comb_mode']=mode; recalc()

            bcomb.on('click', fuel)
            best.on('click', lambda: cash('est', 'Estacionamento', disc_box))
            bod.on('click', lambda: cash('od', 'Outro desconto', disc_box))

            def atualizar_valor_fixo(e):
                # O evento update:model-value traz o valor digitado antes de
                # fixo_i.value estar necessariamente sincronizado no servidor.
                # Atualizamos o componente primeiro e só depois recalculamos.
                novo_valor = e.args
                if isinstance(novo_valor, dict):
                    novo_valor = novo_valor.get('value', novo_valor.get('modelValue', '0,00'))
                elif isinstance(novo_valor, (list, tuple)):
                    novo_valor = novo_valor[0] if novo_valor else '0,00'
                fixo_i.value = novo_valor
                recalc()

            fixo_i.on('update:model-value', atualizar_valor_fixo)
            fixo_i.on('change', lambda e: recalc())
            fixo_i.on('blur', lambda e: recalc())
            obs.on('input', lambda e: recalc())

            def clear_form():
                empresa_i.value=''; rotaid_i.value=''; ref_i.value=''; destino_i.value=''
                fixo_i.value='0,00'; obs.value=''; fields.clear()
                pending_proofs.clear(); render_pending(); render_uploader()
                prod_box.clear(); reimb_box.clear(); disc_box.clear(); recalc()

            def save_route():
                recalc()
                if not state['rota_id']:
                    ui.notify('Informe o ID da rota.', type='warning'); return
                with con() as c:
                    cur = c.execute("""INSERT INTO rotas(
                    data,empresa,rota_id,referencia,destino,fixo,pacotes,vp,ep,paradas,vs,es,km,vk,ek,
                    bonus,outro_extra,pedagio,outro_reembolso,combustivel,combustivel_tratamento,estacionamento,outro_desconto,
                    remuneracao,reembolsos,descontos,receber,resultado,observacao,status,criado)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (state['data'],state['empresa'],state['rota_id'],state['referencia'],state['destino'],
                     state['fixo'],state['pacotes'],state['vp'],state['ep'],state['paradas'],state['vs'],state['es'],
                     state['km'],state['vk'],state['ek'],state['bonus'],state['outro_extra'],state['pedagio'],
                     state['outro_reembolso'],state['combustivel'],state['combustivel_tratamento'],state['estacionamento'],state['outro_desconto'],
                     state['remuneracao'],state['reembolsos'],state['descontos'],state['receber'],state['resultado'],
                     state['observacao'],'Previsto',datetime.now().isoformat()))
                    new_route_id = cur.lastrowid
                    for proof in pending_proofs:
                        c.execute(
                            'INSERT INTO comprovantes(rota_id_db,nome,mime,dados,criado) VALUES(?,?,?,?,?)',
                            (new_route_id, proof['nome'], proof['mime'], proof['dados'], datetime.now().isoformat())
                        )
                # Sincroniza a mesma rota com o Supabase, vinculada ao motorista logado.
                # O SQLite continua ativo nesta fase de validação.
                try:
                    user_id = app.storage.user.get('user_id')
                    access = app.storage.user.get('access_token')
                    refresh = app.storage.user.get('refresh_token')
                    if not user_id or not access or not refresh:
                        raise RuntimeError('Sessão do usuário não encontrada.')

                    cloud = _new_supabase_client()
                    cloud.auth.set_session(access, refresh)

                    fuel_value = state['combustivel']
                    fuel_mode = state['combustivel_tratamento']
                    fuel_discount = fuel_value if fuel_mode == 'Descontado no pagamento' else 0
                    fuel_reimbursement = fuel_value if fuel_mode == 'Reembolsável' else 0

                    payload = {
                        'user_id': user_id,
                        'data': state['data'],
                        'rota_id': state['rota_id'],
                        'rota': state['referencia'],
                        'destino': state['destino'],
                        'valor_base': state['fixo'],
                        'pacotes': state['pacotes'],
                        'valor_pacotes': state['ep'],
                        'paradas': state['paradas'],
                        'valor_paradas': state['es'],
                        'km': state['km'],
                        'valor_km': state['ek'],
                        'outros_adicionais': state['bonus'] + state['outro_extra'],
                        'pedagio_reembolso': state['pedagio'],
                        'outros_reembolsos': state['outro_reembolso'] + fuel_reimbursement,
                        'combustivel_desconto': fuel_discount,
                        'estacionamento_desconto': state['estacionamento'],
                        'outros_descontos': state['outro_desconto'],
                        'observacao': state['observacao'],
                        # Campos detalhados adicionados na migração V2.4
                        'combustivel_valor': state['combustivel'],
                        'combustivel_tratamento': state['combustivel_tratamento'],
                        'bonus': state['bonus'],
                        'outro_adicional': state['outro_extra'],
                        'outro_reembolso': state['outro_reembolso'],
                        'created_at': datetime.now().isoformat(),
                    }
                    inserted = cloud.table('rotas').insert(payload).execute()
                    if not inserted.data:
                        raise RuntimeError('Supabase não retornou a rota salva.')
                    cloud_route_id = inserted.data[0]['id']
                except Exception as e:
                    ui.notify(f'Rota salva localmente, mas não sincronizou com o Supabase: {e}', type='negative', timeout=10000)
                    return

                proof_errors = []
                for proof in pending_proofs:
                    try:
                        upload_cloud_proof(cloud, user_id, cloud_route_id, proof)
                    except Exception as ex:
                        proof_errors.append(f"{proof['nome']}: {ex}")

                saved_total = state['receber']
                refresh_history(); refresh_close(); clear_form()
                if proof_errors:
                    ui.notify('Rota salva no Supabase, mas houve falha em comprovante(s): ' + ' | '.join(proof_errors), type='warning', timeout=12000)
                else:
                    ui.notify(f'Rota salva com sucesso — {money(saved_total)}', type='positive', timeout=7000)

            save_btn.on('click', save_route)
            recalc()

        # ---------- FECHAMENTO ----------
        with ui.tab_panel(close):
            ui.label('Fechamento e conferência').classes('text-2xl font-bold')
            ui.label('Some as rotas de um período e compare com o espelho da empresa.').classes('muted mb-4')

            today = date.today()
            start_default = (today - timedelta(days=14)).isoformat()
            with ui.card().classes('w-full card p-5'):
                with ui.row().classes('w-full items-end gap-4'):
                    ini = ui.input('De', value=start_default).props('type=date outlined').classes('w-44')
                    fim = ui.input('Até', value=today.isoformat()).props('type=date outlined').classes('w-44')
                    empf = ui.input('Empresa (opcional)').props('outlined').classes('w-64')
                    ui.button('7 dias', on_click=lambda: set_period(7)).props('outline no-caps')
                    ui.button('15 dias', on_click=lambda: set_period(15)).props('outline no-caps')
                    ui.button('30 dias', on_click=lambda: set_period(30)).props('outline no-caps')
                    ui.button('Atualizar', icon='refresh', on_click=lambda: refresh_close()).props('no-caps')
            close_box = ui.column().classes('w-full mt-4')
            selected = {}

            def set_period(days):
                fim.value = date.today().isoformat()
                ini.value = (date.today()-timedelta(days=days-1)).isoformat()
                refresh_close()

            def close_rows():
                """Fechamento lê as rotas do usuário autenticado no Supabase."""
                user_id = app.storage.user.get('user_id')
                access = app.storage.user.get('access_token')
                refresh = app.storage.user.get('refresh_token')
                if not user_id or not access or not refresh:
                    raise RuntimeError('Sessão do usuário não encontrada.')
                cloud = _new_supabase_client()
                cloud.auth.set_session(access, refresh)
                q = (cloud.table('rotas').select('*')
                     .eq('user_id', user_id)
                     .gte('data', ini.value)
                     .lte('data', fim.value))
                result = q.order('data').order('id').execute()
                rows = [cloud_route_to_local(r) for r in (result.data or [])]
                # A tabela online atual não possui empresa; o campo é mantido na tela
                # apenas por compatibilidade visual com a versão local.
                return rows

            def refresh_close():
                close_box.clear(); selected.clear()
                rows=close_rows()
                with close_box:
                    if not rows:
                        ui.label('Nenhuma rota nesse período.').classes('muted'); return
                    with ui.card().classes('w-full card p-5'):
                        ui.label(f'{len(rows)} rota(s) encontradas').classes('title')
                        total_lbl=ui.label('Selecionadas: R$ 0,00').classes('text-xl font-bold')
                        checks=[]
                        def total():
                            val=sum(num(r['receber']) for r,ch in checks if ch.value)
                            total_lbl.text='Selecionadas: '+money(val)
                        for r in rows:
                            with ui.row().classes('w-full items-center border-b py-2 gap-4'):
                                ch=ui.checkbox()
                                ui.label(r['data']).classes('w-24')
                                ui.label(r['rota_id']).classes('w-36 font-bold')
                                ui.label(r['empresa'] or '—').classes('w-40')
                                ui.label(r['destino'] or '—').classes('grow')
                                ui.label(money(r['receber'])).classes('font-bold')
                            checks.append((r,ch)); ch.on('update:model-value',lambda e:total())
                        ui.separator()
                        with ui.row().classes('w-full items-end gap-4'):
                            informado=ui.input('Valor informado no espelho (R$)').props('outlined').classes('w-64')
                            obsf=ui.input('Observação do fechamento').props('outlined').classes('grow')
                            def reconcile():
                                chosen=[r for r,ch in checks if ch.value]
                                if not chosen: ui.notify('Selecione ao menos uma rota.',type='warning'); return
                                previsto=sum(num(r['receber']) for r in chosen)
                                inf=num(informado.value); dif=inf-previsto
                                with con() as c:
                                    c.execute('INSERT INTO fechamentos(inicio,fim,empresa,previsto,informado,diferenca,observacao,criado) VALUES(?,?,?,?,?,?,?,?)',
                                              (ini.value,fim.value,empf.value or'',previsto,inf,dif,obsf.value or'',datetime.now().isoformat()))
                                typ='positive' if abs(dif)<0.01 else 'warning'
                                ui.notify(f'Previsto {money(previsto)} | Espelho {money(inf)} | Diferença {money(dif)}',type=typ)
                            ui.button('CONFERIR ESPELHO',icon='fact_check',on_click=reconcile).props('no-caps')
            empf.on('input',lambda e:None)
            refresh_close()

        # ---------- HISTÓRICO ----------
        with ui.tab_panel(hist):
            ui.label('Histórico de rotas').classes('text-2xl font-bold')
            ui.label('Pesquise por ID, destino, empresa ou data. Clique para abrir todos os detalhes.').classes('muted')
            with ui.row().classes('w-full items-end gap-3 mt-3 mobile-stack'):
                search=ui.input('Pesquisar por ID, destino ou rota', placeholder='Ex.: 452452, Rio Claro, R8 AM').props('outlined dense clearable').classes('grow')
                hdate=ui.input('Data exata').props('type=date outlined dense clearable').classes('w-40')
                period=ui.select(['Todos','7 dias','15 dias','30 dias','Personalizado'],value='Todos',label='Período').props('outlined dense').classes('w-40')
                start_date=ui.input('De').props('type=date outlined dense clearable').classes('w-40')
                end_date=ui.input('Até').props('type=date outlined dense clearable').classes('w-40')
                search_btn=ui.button('PESQUISAR',icon='search').props('no-caps').classes('h-10')
                pdf_btn=ui.button('GERAR PDF',icon='picture_as_pdf').props('no-caps color=primary').classes('h-10')
                clear_btn=ui.button('LIMPAR',icon='filter_alt_off').props('outline no-caps').classes('h-10')
            hb=ui.column().classes('w-full mt-4')

            def details(r):
                with ui.dialog() as d, ui.card().classes('w-[820px] max-w-full p-6 dialog-mobile'):
                    ui.label(f"Rota {r['rota_id']}").classes('text-2xl font-bold')
                    ui.label(f"{r['data']} • {r['destino'] or 'Sem destino'}").classes('muted')
                    if r['referencia']: ui.label('Rota: '+r['referencia'])
                    if r['observacao']: ui.label('📝 '+r['observacao']).classes('muted')
                    ui.separator()
                    with ui.element('div').classes('threecol w-full'):
                        with ui.column():
                            ui.label('REMUNERAÇÃO').classes('font-bold')
                            if num(r['fixo']) != 0:
                                ui.label('Valor base: '+money(r['fixo']))
                            if num(r['pacotes']) != 0:
                                ui.label(f"Pacotes: {r['pacotes']:g} × {money(r['vp'])} = {money(r['ep'])}")
                            if num(r['paradas']) != 0:
                                ui.label(f"Paradas: {r['paradas']:g} × {money(r['vs'])} = {money(r['es'])}")
                            if num(r['km']) != 0:
                                ui.label(f"KM: {r['km']:g} km • valor da faixa: {money(r['ek'])}")
                            if num(r['bonus']) != 0:
                                ui.label('Bônus: '+money(r['bonus']))
                            if num(r['outro_extra']) != 0:
                                ui.label('Outro adicional: '+money(r['outro_extra']))
                            ui.label('Total: '+money(r['remuneracao'])).classes('font-bold')
                        with ui.column():
                            ui.label('REEMBOLSOS').classes('font-bold')
                            has_reembolso = False
                            if num(r['pedagio']) != 0:
                                ui.label('Pedágio: '+money(r['pedagio'])); has_reembolso = True
                            if num(r['outro_reembolso']) != 0:
                                ui.label('Outro: '+money(r['outro_reembolso'])); has_reembolso = True
                            if r['combustivel_tratamento'] == 'Reembolsável' and num(r['combustivel']) != 0:
                                ui.label('Combustível: '+money(r['combustivel'])); has_reembolso = True
                            if has_reembolso:
                                ui.label('Total: '+money(r['reembolsos'])).classes('font-bold')
                            else:
                                ui.label('Nenhum reembolso informado.').classes('muted text-sm')
                        with ui.column():
                            ui.label('DESCONTOS / CUSTOS').classes('font-bold')
                            has_custo = False
                            if num(r['combustivel']) != 0:
                                tratamento = r['combustivel_tratamento'] or 'Transportadora'
                                if tratamento not in ('Reembolsável',):
                                    ui.label(f"Combustível: {money(r['combustivel'])} • {tratamento}")
                                    has_custo = True
                            if num(r['estacionamento']) != 0:
                                ui.label('Estacionamento: '+money(r['estacionamento'])); has_custo = True
                            if num(r['outro_desconto']) != 0:
                                ui.label('Outro: '+money(r['outro_desconto'])); has_custo = True
                            if has_custo and num(r['descontos']) != 0:
                                ui.label('Total descontado: '+money(r['descontos'])).classes('font-bold')
                            elif not has_custo:
                                ui.label('Nenhum desconto/custo informado.').classes('muted text-sm')
                    ui.separator()
                    ui.label('📎 COMPROVANTES DA ROTA').classes('font-bold text-lg')
                    ui.label('Guarde prints, fotos ou documentos relacionados a esta rota. Eles podem ajudar na conferência de pagamentos e na comprovação do serviço realizado.').classes('muted text-sm')
                    proof_box = ui.column().classes('w-full gap-2 mt-2')

                    def render_proofs():
                        proof_box.clear()
                        try:
                            client, user_id, proofs = cloud_proofs(r['cloud_id'])
                        except Exception as ex:
                            with proof_box:
                                ui.label(f'Não foi possível carregar comprovantes: {ex}').classes('text-negative')
                            return
                        with proof_box:
                            if not proofs:
                                ui.label('Nenhum comprovante anexado.').classes('muted')
                            for p in proofs:
                                name = p.get('nome_arquivo') or 'comprovante'
                                mime = mimetypes.guess_type(name)[0] or 'application/octet-stream'
                                with ui.row().classes('w-full items-center border rounded-lg p-2 gap-3'):
                                    ui.icon('image' if mime.startswith('image/') else 'description')
                                    ui.label(name).classes('grow')
                                    def view_proof(proof=p, pmime=mime, pname=name):
                                        try:
                                            c, _ = _cloud_session()
                                            raw = cloud_proof_bytes(c, proof['arquivo_path'])
                                            data64 = base64.b64encode(raw).decode('ascii')
                                            data_url = f'data:{pmime};base64,{data64}'
                                            if pmime.startswith('image/'):
                                                with ui.dialog() as vd, ui.card().classes('p-3 dialog-mobile'):
                                                    with ui.row().classes('w-full justify-between items-center'):
                                                        ui.label(pname).classes('font-bold')
                                                        ui.button(icon='close', on_click=vd.close).props('flat round')
                                                    ui.image(data_url).classes('w-full max-h-[78vh] object-contain')
                                                vd.open()
                                            elif pmime == 'application/pdf':
                                                ui.run_javascript(f"window.open({data_url!r}, '_blank')")
                                            else:
                                                ui.notify('Formato não suportado para visualização.', type='warning')
                                        except Exception as ex:
                                            ui.notify(f'Não foi possível abrir: {ex}', type='negative')
                                    ui.button('Visualizar', icon='visibility', on_click=view_proof).props('flat dense no-caps color=primary')
                                    def download_proof(proof=p, pname=name, pmime=mime):
                                        try:
                                            c, _ = _cloud_session()
                                            raw = cloud_proof_bytes(c, proof['arquivo_path'])
                                            ui.download(raw, filename=pname, media_type=pmime)
                                        except Exception as ex:
                                            ui.notify(f'Não foi possível baixar: {ex}', type='negative')
                                    ui.button('Baixar', icon='download', on_click=download_proof).props('flat dense no-caps')
                                    def remove_proof(proof=p):
                                        try:
                                            c, uid = _cloud_session()
                                            c.storage.from_('comprovantes').remove([proof['arquivo_path']])
                                            c.table('comprovantes').delete().eq('id', proof['id']).eq('user_id', uid).execute()
                                            render_proofs(); ui.notify('Comprovante removido.')
                                        except Exception as ex:
                                            ui.notify(f'Não foi possível remover: {ex}', type='negative')
                                    ui.button(icon='delete',on_click=remove_proof).props('flat dense color=negative')

                    async def upload_proof(e):
                        try:
                            raw=await e.file.read()
                            if len(raw)>8*1024*1024:
                                ui.notify('Arquivo maior que 8 MB.',type='warning'); return
                            mime=getattr(e.file,'content_type',None) or ''
                            name=getattr(e.file,'name',None) or 'comprovante'
                            if not (mime.startswith('image/') or mime=='application/pdf'):
                                ui.notify('Envie uma imagem ou PDF.',type='warning'); return
                            c, uid = _cloud_session()
                            upload_cloud_proof(c, uid, r['cloud_id'], {'nome':name,'mime':mime,'dados':raw})
                            render_proofs(); ui.notify('Comprovante anexado ao Supabase.',type='positive')
                        except Exception as ex:
                            ui.notify(f'Não foi possível anexar: {ex}',type='negative')

                    render_proofs()
                    ui.upload(label='ANEXAR COMPROVANTE',on_upload=upload_proof,auto_upload=True,max_file_size=8_000_000).props('accept="image/*,.pdf" flat color=primary').classes('mt-2')
                    ui.separator()
                    ui.label('PREVISÃO A RECEBER: '+money(r['receber'])).classes('text-xl font-bold')
                    with ui.row().classes('w-full justify-end'):
                        def dele():
                            try:
                                user_id = app.storage.user.get('user_id')
                                access = app.storage.user.get('access_token'); refresh = app.storage.user.get('refresh_token')
                                cloud = _new_supabase_client(); cloud.auth.set_session(access, refresh)
                                cloud.table('rotas').delete().eq('id', r['cloud_id']).eq('user_id', user_id).execute()
                                if r.get('id'):
                                    with con() as c: c.execute('DELETE FROM rotas WHERE id=?',(r['id'],))
                                d.close(); refresh_history(); refresh_close(); ui.notify('Rota excluída.', type='positive')
                            except Exception as ex:
                                ui.notify(f'Não foi possível excluir: {ex}', type='negative')
                        ui.button('Excluir',on_click=dele).props('flat color=negative no-caps')
                        ui.button('Fechar',on_click=d.close).props('no-caps')
                d.open()

            def resolve_period():
                choice=period.value or 'Todos'
                if choice=='Personalizado':
                    return start_date.value or '',end_date.value or ''
                if choice in ('7 dias','15 dias','30 dias'):
                    days=int(choice.split()[0])
                    e=date.today(); s=e-timedelta(days=days-1)
                    return s.isoformat(),e.isoformat()
                return '',''

            def update_period_inputs():
                custom=period.value=='Personalizado'
                start_date.set_visibility(custom)
                end_date.set_visibility(custom)

            def cloud_history_rows():
                """Busca o histórico do motorista autenticado diretamente no Supabase."""
                user_id = app.storage.user.get('user_id')
                access = app.storage.user.get('access_token')
                refresh = app.storage.user.get('refresh_token')
                if not user_id or not access or not refresh:
                    raise RuntimeError('Sessão do usuário não encontrada.')

                ini, fim = resolve_period()
                cloud = _new_supabase_client()
                cloud.auth.set_session(access, refresh)
                q = cloud.table('rotas').select('*').eq('user_id', user_id)
                if hdate.value:
                    q = q.eq('data', hdate.value)
                else:
                    if ini: q = q.gte('data', ini)
                    if fim: q = q.lte('data', fim)
                result = q.order('data', desc=True).order('id', desc=True).execute()
                raw_rows = result.data or []
                rows = [cloud_route_to_local(r) for r in raw_rows]

                term = (search.value or '').lower().strip()
                if term:
                    rows = [r for r in rows if
                            term in str(r.get('rota_id') or '').lower() or
                            term in str(r.get('destino') or '').lower() or
                            term in str(r.get('referencia') or '').lower() or
                            term in str(r.get('data') or '').lower()]
                return rows

            def refresh_history():
                hb.clear()
                try:
                    rows = cloud_history_rows()
                except Exception as ex:
                    with hb:
                        ui.label('Não foi possível carregar o histórico online.').classes('text-negative font-bold')
                        ui.label(str(ex)).classes('muted text-sm')
                    return

                with hb:
                    if not rows:
                        ui.label('Nenhuma rota encontrada.').classes('muted')
                        return
                    for r in rows:
                        reemb = num(r['reembolsos']); receber = num(r['receber'])
                        card = ui.card().classes('w-full card p-4 history-row cursor-pointer')
                        card.on('click', lambda e, rr=r: details(rr))
                        with card:
                            with ui.row().classes('w-full items-center gap-5 history-card-row'):
                                with ui.column().classes('gap-0 w-32'):
                                    ui.label(str(r['data'] or '—')).classes('font-bold')
                                with ui.column().classes('gap-0 grow'):
                                    ui.label('Rota '+str(r['rota_id'] or '—')).classes('font-bold text-lg')
                                    destino = str(r['destino'] or 'Sem destino')
                                    referencia = str(r['referencia'] or '')
                                    ui.label(destino + ((' • '+referencia) if referencia else '')).classes('muted')
                                if reemb:
                                    ui.label('Reemb. '+money(reemb)).classes('w-36 history-money')
                                ui.label('A receber '+money(receber)).classes('font-bold w-44 history-money')
                                ui.icon('cloud_done').classes('text-positive')

            def generate_history_pdf():
                try:
                    rows = cloud_history_rows()
                    if not rows:
                        ui.notify('Nenhuma rota encontrada para este filtro.', type='warning'); return
                    ini_pdf, fim_pdf = resolve_period()
                    buf = BytesIO()
                    doc = SimpleDocTemplate(buf,pagesize=A4,rightMargin=14*mm,leftMargin=14*mm,topMargin=14*mm,bottomMargin=14*mm)
                    styles=getSampleStyleSheet()
                    title=ParagraphStyle('rt_cloud',parent=styles['Title'],fontName='Helvetica-Bold',fontSize=18,leading=22,alignment=TA_CENTER)
                    body=ParagraphStyle('rb_cloud',parent=styles['BodyText'],fontSize=9,leading=12)
                    small=ParagraphStyle('rs_cloud',parent=styles['BodyText'],fontSize=8,leading=10)
                    story=[Paragraph('RotaOS - Fechamento de rotas',title),Spacer(1,4*mm)]
                    periodo_txt = f"Periodo: {ini_pdf or hdate.value or 'inicio'} a {fim_pdf or hdate.value or 'fim'}" if (ini_pdf or fim_pdf or hdate.value) else 'Periodo: todas as rotas filtradas'
                    story += [Paragraph(periodo_txt,body),Paragraph(f"Quantidade de rotas: {len(rows)}",body),Spacer(1,5*mm)]
                    tr=tre=td=ta=0.0
                    for i,r in enumerate(rows,1):
                        tr+=num(r['remuneracao']); tre+=num(r['reembolsos']); td+=num(r['descontos']); ta+=num(r['receber'])
                        prod,reimb,disc=pdf_route_parts(r)
                        story.append(Paragraph(f"<b>{i}. Rota {r['rota_id'] or '-'}</b>",body))
                        meta=[r['data'] or '-']
                        if r['referencia']: meta.append('Rota: '+r['referencia'])
                        if r['destino']: meta.append('Destino/regiao: '+r['destino'])
                        story.append(Paragraph(' | '.join(meta),small))
                        cells=[]
                        if prod: cells.append([Paragraph('<b>REMUNERACAO</b>',small),Paragraph('<br/>'.join(prod)+f"<br/><b>Total: {money(r['remuneracao'])}</b>",small)])
                        if reimb: cells.append([Paragraph('<b>REEMBOLSOS</b>',small),Paragraph('<br/>'.join(reimb)+f"<br/><b>Total: {money(r['reembolsos'])}</b>",small)])
                        if disc: cells.append([Paragraph('<b>DESCONTOS</b>',small),Paragraph('<br/>'.join(disc)+f"<br/><b>Total: {money(r['descontos'])}</b>",small)])
                        if cells:
                            t=Table(cells,colWidths=[38*mm,137*mm]); t.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('BACKGROUND',(0,0),(0,-1),colors.HexColor('#F2F6FA')),('BOX',(0,0),(-1,-1),.4,colors.HexColor('#C9D3DD')),('INNERGRID',(0,0),(-1,-1),.25,colors.HexColor('#D9E1E8')),('PADDING',(0,0),(-1,-1),5)])); story += [Spacer(1,2*mm),t]
                        story += [Spacer(1,2*mm),Paragraph(f"<b>A receber: {money(r['receber'])}</b>",body)]
                        if r['observacao']: story.append(Paragraph('Observacao: '+r['observacao'],small))
                        story.append(Spacer(1,5*mm))
                    summary=Table([['RESUMO DO PERIODO',''],['Remuneracao',money(tr)],['Reembolsos',money(tre)],['Descontos',money(td)],['TOTAL A RECEBER',money(ta)]],colWidths=[95*mm,80*mm])
                    summary.setStyle(TableStyle([('SPAN',(0,0),(1,0)),('BACKGROUND',(0,0),(1,0),colors.HexColor('#DCEEFF')),('FONTNAME',(0,0),(1,0),'Helvetica-Bold'),('FONTNAME',(0,4),(1,4),'Helvetica-Bold'),('ALIGN',(1,1),(1,-1),'RIGHT'),('BOX',(0,0),(-1,-1),.6,colors.HexColor('#7D9AB5')),('INNERGRID',(0,1),(-1,-1),.3,colors.HexColor('#CCD7E0')),('PADDING',(0,0),(-1,-1),7)])); story.append(summary)
                    doc.build(story)
                    filename=f"RotaOS_fechamento_{ini_pdf or hdate.value or 'inicio'}_{fim_pdf or hdate.value or 'fim'}.pdf"
                    ui.download(buf.getvalue(), filename=filename)
                    buf.close()
                except Exception as ex:
                    ui.notify(f'Não foi possível gerar o PDF online: {ex}', type='negative', timeout=10000)

            def clear_history_filters():
                search.value=''; hdate.value=None; period.value='Todos'
                start_date.value=None; end_date.value=None
                update_period_inputs(); refresh_history()

            search_btn.on('click',lambda:refresh_history())
            pdf_btn.on('click',generate_history_pdf)
            clear_btn.on('click',clear_history_filters)
            search.on('keydown.enter',lambda e:refresh_history())
            hdate.on('keydown.enter',lambda e:refresh_history())
            period.on('update:model-value',lambda e:(update_period_inputs(),refresh_history()))
            start_date.on('change',lambda e:refresh_history())
            end_date.on('change',lambda e:refresh_history())
            update_period_inputs()
            refresh_history()

        # ---------- FAIXAS ----------
        with ui.tab_panel(cfg):
            ui.label('Faixas de remuneração').classes('text-2xl font-bold')
            ui.label('Pacotes e paradas usam valor por unidade. KM usa valor fixo do bloco/faixa atingida.').classes('muted mb-4')
            areas={}
            def render(tipo):
                a=areas[tipo];a.clear()
                with a:
                    try:
                        rows = cloud_ranges(tipo)
                    except Exception as ex:
                        rows = []
                        ui.label(f'Não foi possível carregar faixas: {ex}').classes('text-negative')
                    if not rows: ui.label('Nenhuma faixa cadastrada.').classes('muted')
                    for r in rows:
                        with ui.row().classes('w-full justify-between border rounded-lg p-2'):
                            ui.label(
                                f"{r['minimo']:g} até < {r['maximo']:g} → {money(r['valor'])}"
                                + ('' if tipo == 'km' else '/un.')
                            ).classes('font-bold')
                            def dele(i=r['id'],tt=tipo):
                                try:
                                    delete_cloud_range(i)
                                    render(tt)
                                except Exception as ex:
                                    ui.notify(f'Não foi possível excluir: {ex}', type='negative')
                            ui.button(icon='delete',on_click=dele).props('flat dense color=negative')
            with ui.element('div').classes('threecol w-full'):
                for tipo,title in [('pacotes','📦 Pacotes'),('paradas','📍 Paradas'),('km','🚗 KM')]:
                    with ui.card().classes('card p-4 w-full'):
                        ui.label(title).classes('title')
                        areas[tipo]=ui.column().classes('w-full');render(tipo)
                        with ui.row().classes('w-full items-end gap-2'):
                            mi=ui.input('De').props('outlined dense').classes('w-20')
                            ma=ui.input('Até').props('outlined dense').classes('w-20')
                            val=ui.input('Valor faixa R$' if tipo == 'km' else 'R$/un.').props('outlined dense').classes('w-28')
                            def add(tipo=tipo,mi=mi,ma=ma,val=val):
                                try:
                                    add_cloud_range(tipo,mi.value,ma.value,val.value)
                                    mi.value='';ma.value='';val.value='';render(tipo)
                                    ui.notify('Faixa salva.',type='positive')
                                except Exception as e: ui.notify(str(e),type='negative')
                            ui.button('Adicionar',on_click=add).props('no-caps')


@ui.page('/')
def index():
    if _restore_session() is None:
        render_login()
        return
    render_rotaos()

ui.run(
    host='0.0.0.0',
    port=int(os.environ.get('PORT',8080)),
    title='RotaOS V2.6 ONLINE',
    favicon='🚚',
    reload=False,
    storage_secret=os.environ.get('ROTAOS_STORAGE_SECRET', 'rotaos-local-dev-session-secret'),
)
