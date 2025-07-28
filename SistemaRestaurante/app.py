import re
import os
from datetime import datetime
from collections import Counter, defaultdict

import pytz
from flask import (
    Flask, render_template, request,
    redirect, url_for, flash, jsonify
)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin,
    login_user, login_required,
    current_user, logout_user
)
from flask_socketio import SocketIO
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'dev_secret_64bytes')
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///restaurante.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db       = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*")

# ─── MODELS ─────────────────────────────────────────────────────────────

class User(UserMixin, db.Model):
    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    role          = db.Column(db.String(20), nullable=False)
    def check_password(self, pwd):
        return check_password_hash(self.password_hash, pwd)

class Pedido(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    mesa       = db.Column(db.Integer, nullable=False)
    garcom     = db.Column(db.String(50), nullable=False)
    delivery   = db.Column(db.Boolean, default=False)
    endereco   = db.Column(db.String(200), nullable=True)
    numero     = db.Column(db.String(50), nullable=True)
    status     = db.Column(db.String(20), default='Pendente')
    criado_em  = db.Column(db.DateTime, default=datetime.utcnow)
    pratos     = db.Column(db.String(300), nullable=False)
    bebidas    = db.Column(db.String(300), nullable=False)
    porcao     = db.Column(db.String(100), nullable=False)
    observacao = db.Column(db.String(200), nullable=True)

# ─── LOGIN SETUP ────────────────────────────────────────────────────────

login_manager = LoginManager(app)
login_manager.login_view = 'login'

@login_manager.user_loader
def load_user(uid):
    return User.query.get(int(uid))

def role_required(*roles):
    def deco(f):
        @login_required
        def wrapped(*args, **kwargs):
            if current_user.role not in roles:
                flash("Permissão negada.", "danger")
                return redirect(url_for('login'))
            return f(*args, **kwargs)
        wrapped.__name__ = f.__name__
        return wrapped
    return deco

# ─── CARDÁPIO ──────────────────────────────────────────────────────────

cardapio = {
    'pratos': [
        {'nome':'Nenhum Prato','preco':0},
        {'nome':'Filé de Frango','preco':20},
        {'nome':'Bife','preco':20},
        {'nome':'Ovo','preco':20},
    ],
    'bebidas': [
        {'nome':'Nenhuma Bebida','preco':0},
        {'nome':'Coca 2L','preco':15},
        {'nome':'Água','preco':2},
        {'nome':'Suco','preco':10},
    ],
    'porcoes': [
        {'nome':'Nenhuma Porção','preco':0},
        {'nome':'Batata Frita','preco':25},
    ]
}

# ─── ROTAS ─────────────────────────────────────────────────────────────

@app.route('/')
def root():
    return redirect(url_for('login'))

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        u = User.query.filter_by(username=request.form['username']).first()
        if u and u.check_password(request.form['password']):
            login_user(u)
            if u.role == 'garcom':
                return redirect(url_for('garcom'))
            if u.role == 'cozinha':
                return redirect(url_for('cozinha'))
            if u.role == 'caixa':
                return redirect(url_for('pedidos'))
        flash("Usuário ou senha inválidos.", "danger")
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

# → Garçom
@app.route('/garcom')
@role_required('garcom')
def garcom():
    return render_template('garcom.html', cardapio=cardapio)

@app.route('/garcom/pedido', methods=['POST'])
@role_required('garcom')
def fazer_pedido():
    mesa        = int(request.form['mesa'])
    garcomN     = current_user.username
    is_delivery = 'delivery' in request.form
    end         = request.form.get('endereco') if is_delivery else None
    num         = request.form.get('numero')   if is_delivery else None

    nomes_p = request.form.getlist('prato_nome')
    qts_p   = request.form.getlist('prato_qtd')
    lista_p = [f"{n} ({q})" for n,q in zip(nomes_p,qts_p)
               if n!='Nenhum Prato' and int(q)>0]

    nomes_b = request.form.getlist('bebida_nome')
    qts_b   = request.form.getlist('bebida_qtd')
    lista_b = [f"{n} ({q})" for n,q in zip(nomes_b,qts_b)
               if n!='Nenhuma Bebida' and int(q)>0]

    porcao     = request.form['porcao']
    observacao = request.form.get('observacao','')

    novo = Pedido(
        mesa=mesa, garcom=garcomN,
        delivery=is_delivery, endereco=end, numero=num,
        pratos=', '.join(lista_p)  or 'Sem Prato',
        bebidas=', '.join(lista_b) or 'Sem Bebida',
        porcao=porcao, observacao=observacao
    )
    db.session.add(novo)
    db.session.commit()

    hora_sp = novo.criado_em.replace(tzinfo=pytz.utc)\
              .astimezone(pytz.timezone('America/Sao_Paulo'))\
              .strftime('%H:%M:%S')
    payload = {
        'id': novo.id, 'mesa': novo.mesa, 'garcom': novo.garcom,
        'delivery': novo.delivery, 'endereco': novo.endereco,
        'numero': novo.numero, 'pratos': novo.pratos,
        'bebidas': novo.bebidas, 'porcao': novo.porcao,
        'observacao': novo.observacao, 'hora': hora_sp
    }
    socketio.emit('new_order', payload)
    return redirect(url_for('garcom'))

# → Cozinha
@app.route('/cozinha')
@role_required('cozinha')
def cozinha():
    sp = pytz.timezone('America/Sao_Paulo')
    pendentes = []
    for p in Pedido.query.filter_by(status='Pendente').all():
        dt_sp = p.criado_em.replace(tzinfo=pytz.utc).astimezone(sp)
        pendentes.append({
            'id': p.id, 'mesa': p.mesa, 'garcom': p.garcom,
            'delivery': p.delivery, 'endereco': p.endereco,
            'numero': p.numero, 'pratos': p.pratos,
            'bebidas': p.bebidas, 'porcao': p.porcao,
            'observacao': p.observacao,
            'hora': dt_sp.strftime('%H:%M:%S')
        })
    return render_template('cozinha.html', pedidos=pendentes)

@app.route('/cozinha/pronto/<int:pid>', methods=['POST'])
@role_required('cozinha')
def marcar_pronto(pid):
    p = Pedido.query.get_or_404(pid)
    p.status = 'Pronto'
    db.session.commit()
    socketio.emit('order_pronto', {'id': pid})
    return redirect(url_for('cozinha'))

# → Caixa
@app.route('/pedidos')
@role_required('caixa')
def pedidos():
    detalhados = []
    for p in Pedido.query.filter(Pedido.status!='Pago').all():
        itens, total = [], 0
        # pratos
        for pr in p.pratos.split(', '):
            m = re.match(r'(.+?) \((\d+)\)', pr)
            if m:
                nome, qtd = m.group(1), int(m.group(2))
                preco = next(x['preco'] for x in cardapio['pratos'] if x['nome']==nome)
                sub = preco*qtd; total += sub
                itens.append(f"{nome} ({qtd}) - R$ {sub}")
        # bebidas
        for be in p.bebidas.split(', '):
            m = re.match(r'(.+?) \((\d+)\)', be)
            if m:
                nome, qtd = m.group(1), int(m.group(2))
                preco = next(x['preco'] for x in cardapio['bebidas'] if x['nome']==nome)
                sub = preco*qtd; total += sub
                itens.append(f"{nome} ({qtd}) - R$ {sub}")
        # porção
        if p.porcao!='Nenhuma Porção':
            preco = next(x['preco'] for x in cardapio['porcoes'] if x['nome']==p.porcao)
            total += preco
            itens.append(f"{p.porcao} - R$ {preco}")
        detalhados.append({'pedido':p, 'itens':", ".join(itens), 'total':total})
    return render_template('pedidos.html', pedidos_detalhados=detalhados)

@app.route('/caixa/pagar/<int:pid>', methods=['POST'])
@role_required('caixa')
def marcar_pago(pid):
    p = Pedido.query.get_or_404(pid)
    p.status = 'Pago'
    db.session.commit()
    socketio.emit('order_paid', {'id': pid})
    return redirect(url_for('pedidos'))

# → Dashboard
@app.route('/dashboard')
@role_required('caixa')
def dashboard():
    return render_template('dashboard.html')

@app.route('/api/dashboard')
@role_required('caixa')
def api_dashboard():
    pagos = Pedido.query.filter_by(status='Pago').all()
    sales_by_day = defaultdict(float)
    item_counter = Counter()
    utc, sp = pytz.utc, pytz.timezone('America/Sao_Paulo')
    for p in pagos:
        dt_sp = utc.localize(p.criado_em).astimezone(sp)
        day = dt_sp.strftime('%Y-%m-%d'); total = 0
        # pratos
        for pr in p.pratos.split(', '):
            m = re.match(r'(.+?) \((\d+)\)', pr)
            if m:
                nome, qtd = m.group(1), int(m.group(2))
                preco = next(x['preco'] for x in cardapio['pratos'] if x['nome']==nome)
                total += preco*qtd; item_counter[nome]+=qtd
        # bebidas
        for be in p.bebidas.split(', '):
            m = re.match(r'(.+?) \((\d+)\)', be)
            if m:
                nome, qtd = m.group(1), int(m.group(2))
                preco = next(x['preco'] for x in cardapio['bebidas'] if x['nome']==nome)
                total += preco*qtd; item_counter[nome]+=qtd
        # porção
        if p.porcao!='Nenhuma Porção':
            preco = next(x['preco'] for x in cardapio['porcoes'] if x['nome']==p.porcao)
            total += preco; item_counter[p.porcao]+=1
        sales_by_day[day] += total
    sales = [{'date':d,'total':sales_by_day[d]} for d in sorted(sales_by_day)]
    items = [{'name':n,'count':c} for n,c in item_counter.most_common()]
    return jsonify({'sales':sales,'items':items})

def init_db():
    db.create_all()
    if not User.query.first():
        users = [
            ('cozinha','1212','cozinha'),
            ('sala','7878','garcom'),
            ('admin','7458','caixa'),
        ]
        for u,pwd,role in users:
            db.session.add(User(
                username=u,
                password_hash=generate_password_hash(pwd),
                role=role
            ))
        db.session.commit()

if __name__ == '__main__':
    with app.app_context():
        init_db()
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)
