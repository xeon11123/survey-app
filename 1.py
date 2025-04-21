from flask import Flask, render_template, request, redirect, session, url_for, abort
from flask_session import Session
from sqlalchemy import create_engine, Column, Integer, String, ForeignKey, DateTime, JSON
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import os, datetime, uuid

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'replace-with-secure-key')
app.config['SESSION_TYPE'] = 'filesystem'
Session(app)

# DB 설정 (SQLite)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'survey.db')
engine = create_engine(f'sqlite:///{DB_PATH}', connect_args={'check_same_thread': False})
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

# 모델 정의
class Respondent(Base):
    __tablename__ = 'respondents'
    id = Column(Integer, primary_key=True)
    ip = Column(String, index=True)
    user_agent = Column(String)
    cookie_id = Column(String, unique=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    ranking = Column(JSON)

class Comparison(Base):
    __tablename__ = 'comparisons'
    id = Column(Integer, primary_key=True)
    respondent_id = Column(Integer, ForeignKey('respondents.id'))
    item_a = Column(Integer)
    item_b = Column(Integer)
    result = Column(String)

class Aggregate(Base):
    __tablename__ = 'aggregate'
    category_id = Column(Integer, primary_key=True)
    total_rank = Column(Integer, default=0)
    votes = Column(Integer, default=0)

Base.metadata.create_all(engine)

# 카테고리 리스트
CATEGORIES = [
    "경희 한의","그외 한의","서울 약학","수도권 약학","그외 약학",
    "서울 수의","건국 수의","그외 수의",
    "서울대 상위","서울대 중위","서울대 하위",
    "계약(연고서성한 반도체)",
    "연세대 상위","연세대 중위","연세대 하위",
    "고려대 상위","고려대 중위","고려대 하위"
]
N = len(CATEGORIES)

# DSU & Preference Graph 유틸리티
class DSU:
    def __init__(self, n): self.parent=list(range(n))
    def find(self,x): return x if self.parent[x]==x else self.find(self.parent[x])
    def union(self,a,b):
        a,b = self.find(a), self.find(b)
        if a!=b: self.parent[b]=a

class PrefGraph:
    def __init__(self,n):
        self.n = n
        self.adj = [set() for _ in range(n)]
    def add_edge(self,a,b): self.adj[a].add(b)
    def has_path(self,a,b):
        visited = [False]*self.n
        def dfs(u):
            if u==b: return True
            visited[u]=True
            for v in self.adj[u]:
                if not visited[v] and dfs(v): return True
            return False
        return dfs(a)

# 중복 참여 방지
@app.before_request
def block_duplicate():
    if request.path=='/' and 'respondent_id' in session:
        return redirect(url_for('already'))

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/start', methods=['POST'])
def start():
    db = SessionLocal()
    ip = request.remote_addr
    ua = request.headers.get('User-Agent')
    existing = db.query(Respondent).filter((Respondent.ip==ip)|(Respondent.cookie_id==session.get('cookie_id'))).first()
    if existing:
        return redirect(url_for('already'))
    cookie_id = str(uuid.uuid4())
    r = Respondent(ip=ip, user_agent=ua, cookie_id=cookie_id)
    db.add(r); db.commit()
    session['respondent_id']=r.id
    session['cookie_id']=cookie_id
    session['dsu_parent']=list(range(N))
    session['edges']=[[] for _ in range(N)]
    return redirect(url_for('survey'))

@app.route('/already')
def already():
    return "이미 설문에 참여하셨습니다."

@app.route('/survey', methods=['GET','POST'])
def survey():
    if 'respondent_id' not in session:
        return redirect(url_for('index'))

    # 상태 불러오기
    dsu = DSU(N)
    dsu.parent = session['dsu_parent']
    pg = PrefGraph(N)
    pg.adj = [set(lst) for lst in session['edges']]

    if request.method=='POST':
        a = int(request.form['a'])
        b = int(request.form['b'])
        res = request.form['result']
        db = SessionLocal()
        comp = Comparison(respondent_id=session['respondent_id'], item_a=a, item_b=b, result=res)
        db.add(comp); db.commit()
        if res=='equal':
            dsu.union(a,b)
        elif res=='a':
            pg.add_edge(a,b)
        else:
            pg.add_edge(b,a)
        session['dsu_parent']=dsu.parent
        session['edges']=[list(s) for s in pg.adj]

    # 다음 질문 찾기
    sets = {}
    for i in range(N):
        root = dsu.find(i)
        sets.setdefault(root, []).append(i)
    roots = list(sets.keys())
    next_pair=None
    for i in range(len(roots)):
        for j in range(i+1, len(roots)):
            u,v = roots[i], roots[j]
            if not pg.has_path(u,v) and not pg.has_path(v,u):
                next_pair=(u,v)
                break
        if next_pair: break

    if not next_pair:
        # 순위 확정
        indeg={r:0 for r in roots}
        for u in roots:
            for v in pg.adj[u]:
                if v in indeg: indeg[v]+=1
        Q=[u for u in roots if indeg[u]==0]
        order=[]
        while Q:
            u=Q.pop(0)
            order.append(u)
            for v in pg.adj[u]:
                if v in indeg:
                    indeg[v]-=1
                    if indeg[v]==0: Q.append(v)
        ranks={}
        for idx,root in enumerate(order,1):
            for i in sets[root]:
                ranks[i]=idx
        db = SessionLocal()
        respondent = db.query(Respondent).get(session['respondent_id'])
        respondent.ranking = ranks
        db.commit()
        session.clear()
        return redirect(url_for('thankyou'))

    a,b = next_pair
    return render_template('survey.html', a=a, b=b, categories=CATEGORIES)

@app.route('/thankyou')
def thankyou():
    admin_token = os.environ.get('ADMIN_TOKEN','admintoken123')
    summary_link = url_for('results_summary', _external=True)
    detail_link  = url_for('results_detail', token=admin_token, _external=True)
    return render_template('thankyou.html', summary_link=summary_link, detail_link=detail_link)

@app.route('/results/summary')
def results_summary():
    db = SessionLocal()
    agg = db.query(Aggregate).all()
    if not agg:
        for i in range(N):
            db.add(Aggregate(category_id=i))
        db.commit()
        agg = db.query(Aggregate).all()
    for a in agg:
        a.total_rank=0; a.votes=0
    db.commit()
    respondents = db.query(Respondent).filter(Respondent.ranking!=None).all()
    for r in respondents:
        for cat,rank in r.ranking.items():
            ag = db.query(Aggregate).get(int(cat))
            ag.total_rank += rank
            ag.votes += 1
    db.commit()
    summary = [(CATEGORIES[a.category_id], round(a.total_rank/a.votes,2) if a.votes else None) for a in agg]
    return render_template('summary.html', summary=summary)

@app.route('/results/detail')
def results_detail():
    token = request.args.get('token')
    if token != os.environ.get('ADMIN_TOKEN','admintoken123'):
        abort(403)
    db = SessionLocal()
    respondents = db.query(Respondent).filter(Respondent.ranking!=None).all()
    return render_template('detail.html', respondents=respondents, categories=CATEGORIES)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
