from nicegui import ui, app
from fastapi import HTTPException
from fastapi.responses import Response
from urllib.parse import quote, urlencode
import sqlite3, os, base64
from io import BytesIO
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from datetime import date, datetime, timedelta

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
    ui.label('🚚 RotaOS V2.0.1').classes('text-xl font-bold')
    ui.label('Produção • Recebimentos • Conferência').classes('text-sm')

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
        best.on('click', lambda: cash('est','Estacionamento',disc_box))
        bod.on('click', lambda: cash('od','Outro desconto',disc_box))
        fixo_i.on('input', lambda e: recalc())
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
            refresh_history(); refresh_close(); clear_form()
            ui.notify('Rota salva.', type='positive')

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
            q='SELECT * FROM rotas WHERE data BETWEEN ? AND ?'
            p=[ini.value,fim.value]
            if empf.value:
                q+=' AND lower(empresa) LIKE ?'; p.append('%'+empf.value.lower()+'%')
            q+=' ORDER BY data, id'
            with con() as c: return c.execute(q,p).fetchall()

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
                    with con() as c:
                        proofs=c.execute('SELECT id,nome,mime,dados,length(dados) tamanho FROM comprovantes WHERE rota_id_db=? ORDER BY id DESC',(r['id'],)).fetchall()
                    with proof_box:
                        if not proofs: ui.label('Nenhum comprovante anexado.').classes('muted')
                        for p in proofs:
                            with ui.row().classes('w-full items-center border rounded-lg p-2 gap-3'):
                                ui.icon('image' if (p['mime'] or '').startswith('image/') else 'description')
                                ui.label(p['nome']).classes('grow')
                                ui.label(f"{(p['tamanho'] or 0)/1024:.1f} KB").classes('muted text-xs')
                                def view_proof(proof=p):
                                    mime = proof['mime'] or 'application/octet-stream'
                                    data64 = base64.b64encode(proof['dados']).decode('ascii')
                                    data_url = f'data:{mime};base64,{data64}'
                                    if mime.startswith('image/'):
                                        with ui.dialog() as vd, ui.card().classes('p-3 dialog-mobile'):
                                            with ui.row().classes('w-full justify-between items-center'):
                                                ui.label(proof['nome']).classes('font-bold')
                                                ui.button(icon='close', on_click=vd.close).props('flat round')
                                            ui.image(data_url).classes('w-full max-h-[78vh] object-contain')
                                        vd.open()
                                    elif mime == 'application/pdf':
                                        ui.run_javascript(f"window.open({data_url!r}, '_blank')")
                                    else:
                                        ui.notify('Formato não suportado para visualização.', type='warning')
                                ui.button('Visualizar', icon='visibility', on_click=view_proof).props('flat dense no-caps color=primary')
                                def download_proof(pid=p['id']):
                                    ui.run_javascript(
                                        f"window.location.assign('/comprovantes/{pid}/download')"
                                    )
                                ui.button('Baixar', icon='download', on_click=download_proof).props('flat dense no-caps')
                                def remove_proof(pid=p['id']):
                                    with con() as c: c.execute('DELETE FROM comprovantes WHERE id=?',(pid,))
                                    render_proofs(); ui.notify('Comprovante removido.')
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
                        with con() as c: c.execute('INSERT INTO comprovantes(rota_id_db,nome,mime,dados,criado) VALUES(?,?,?,?,?)',(r['id'],name,mime,raw,datetime.now().isoformat()))
                        render_proofs(); ui.notify('Comprovante anexado.',type='positive')
                    except Exception as ex: ui.notify(f'Não foi possível anexar: {ex}',type='negative')

                render_proofs()
                ui.upload(label='ANEXAR COMPROVANTE',on_upload=upload_proof,auto_upload=True,max_file_size=8_000_000).props('accept="image/*,.pdf" flat color=primary').classes('mt-2')
                ui.separator()
                ui.label('PREVISÃO A RECEBER: '+money(r['receber'])).classes('text-xl font-bold')
                with ui.row().classes('w-full justify-end'):
                    def dele():
                        with con() as c:c.execute('DELETE FROM rotas WHERE id=?',(r['id'],))
                        d.close();refresh_history();refresh_close();ui.notify('Rota excluída.')
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

        def refresh_history():
            hb.clear()
            ini,fim=resolve_period()
            rows=period_rows(search.value or '',hdate.value or '',ini,fim)
            with hb:
                if not rows: ui.label('Nenhuma rota encontrada.').classes('muted')
                for r in rows:
                    with ui.card().classes('w-full card p-4 history-row cursor-pointer') as card:
                        card.on('click',lambda e,r=r:details(r))
                        with ui.row().classes('w-full items-center gap-5 history-card-row'):
                            with ui.column().classes('gap-0 w-32'):
                                ui.label(r['data']).classes('font-bold')
                            with ui.column().classes('gap-0 grow'):
                                ui.label('Rota '+(r['rota_id'] or '—')).classes('font-bold text-lg')
                                ui.label((r['destino'] or 'Sem destino')+((' • '+r['referencia']) if r['referencia'] else '')).classes('muted')
                            if num(r['reembolsos']):
                                ui.label('Reemb. '+money(r['reembolsos'])).classes('w-36 history-money')
                            ui.label('A receber '+money(r['receber'])).classes('font-bold w-44 history-money')
                            ui.icon('chevron_right')

        def generate_history_pdf():
            ini,fim=resolve_period()
            params=urlencode({'search':search.value or '','date_filter':hdate.value or '','start':ini,'end':fim})
            ui.run_javascript(f"window.location.assign('/historico/pdf?{params}')")

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
                with con() as c: rows=c.execute('SELECT * FROM faixas WHERE tipo=? ORDER BY minimo',(tipo,)).fetchall()
                if not rows: ui.label('Nenhuma faixa cadastrada.').classes('muted')
                for r in rows:
                    with ui.row().classes('w-full justify-between border rounded-lg p-2'):
                        ui.label(
                            f"{r['minimo']:g} até < {r['maximo']:g} → {money(r['valor'])}"
                            + ('' if tipo == 'km' else '/un.')
                        ).classes('font-bold')
                        def dele(i=r['id'],tt=tipo):
                            with con() as c:c.execute('DELETE FROM faixas WHERE id=?',(i,))
                            render(tt)
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
                                add_range(tipo,mi.value,ma.value,val.value)
                                mi.value='';ma.value='';val.value='';render(tipo)
                                ui.notify('Faixa salva.',type='positive')
                            except Exception as e: ui.notify(str(e),type='negative')
                        ui.button('Adicionar',on_click=add).props('no-caps')

ui.run(
    host='0.0.0.0',
    port=int(os.environ.get('PORT',8080)),
    title='RotaOS V2.0.1',
    favicon='🚚',
    reload=False,
)
