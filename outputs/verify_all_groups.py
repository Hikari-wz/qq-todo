"""Synthetic all-group migration, queue, association and history regression."""
import json
import tempfile
import time
from pathlib import Path
from all_groups import Catchup, related_cards
from napcat_pilot import Pilot
from trial import read, save, set_manual_status
from verify_napcat import PROFILE, GROUP, event, row, response


def main():
    with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent.parent/'work') as tmp:
        path=Path(tmp)/'state.json'
        old=Pilot(path,GROUP,PROFILE,9931)
        old.receive(event(1,'旧任务'))
        old.extract=lambda ctx:response([row(ctx['current'],'todo')])
        assert old.process_next()
        state=read(path); original=next(iter(state['cards']))
        set_manual_status(state,original,'done'); save(path,state)
        pilot=Pilot(path,None,PROFILE,9931,True)
        enabled=read(path)['enabled_at']
        original_card=read(path)['cards'][original]
        def msg(mid,group,text,at=enabled):
            return dict(event(mid,text,group),time=at,user_id=900099,message_seq=str(mid),group_name='测试群')
        pilot.receive(msg(2,GROUP,'三班请提交今日小结。',enabled-1))
        assert not read(path)['pending']
        a=msg(2,GROUP,'三班请提交甲课程小结。')
        b=msg(2,'900002','三班请提交乙课程小结。')
        pilot.receive(a); pilot.receive(b); pilot.receive(a)
        contexts=[]
        def extract(ctx):
            contexts.append(ctx)
            return response([dict(row(ctx['current'],'todo'),title='课程小结',action='提交小结')])
        pilot.extract=extract
        assert pilot.process_next() and pilot.process_next()
        state=read(path)
        assert len(state['cards'])==3 and state['cards'][original]==original_card
        assert not contexts[1]['cards'] and not contexts[1]['history']
        assert state['stats']['duplicate']==1
        newkeys=[k for k in state['cards'] if k!=original]
        set_manual_status(state,newkeys[0],'dismissed'); save(path,state)
        pilot=Pilot(path,None,PROFILE,9931,True)
        pilot.receive(b); assert not read(path)['pending']
        # A guessed cross-group link is rejected, not merged.
        pilot.receive(msg(3,'900003','三班请交丙课程的小结。'))
        pilot.extract=lambda ctx:response([dict(row(ctx['current'],'todo'),link=newkeys[0])])
        assert pilot.process_next()
        state=read(path)
        assert len(state['cards'])==4 and state['cards'][newkeys[0]]['manual_status']=='dismissed'
        assert list(state['cards'].values())[-1]['decision']=='review'
        # An explicit same-notice retransmission provides a cross-group candidate.
        same=msg(4,'900004','三班请提交甲课程小结。'+('说明。'*10))
        pilot.receive(same); pilot.extract=lambda ctx:response([row(ctx['current'],'todo')]); assert pilot.process_next()
        state=read(path)
        from napcat_pilot import normalize
        assert related_cards(state,normalize(dict(same,group_id='900005'), '900005'))
        # Membership discovery, per-group progress and page bounds.
        sent=[]; c=Catchup(pilot,sent.append,900000)
        c.groups([{'group_id':GROUP},{'group_id':'900002'}])
        assert c.active['since']==enabled
        batch=[msg(100+i,GROUP,'历史通知',enabled+1) for i in range(100)]
        c.response({'echo':sent[-1]['echo'],'status':'ok','data':{'messages':batch}})
        assert c.active['pages']==1 and len(sent)==2
        c.response({'echo':sent[-1]['echo'],'status':'ok','data':{'messages':[msg(99,GROUP,'窗口之前',enabled-1)]}})
        assert c.active['gid']=='900002'
        repeated=[msg(200+i,'900002','历史通知',enabled+1) for i in range(100)]
        c.response({'echo':sent[-1]['echo'],'status':'ok','data':{'messages':repeated}})
        c.response({'echo':sent[-1]['echo'],'status':'ok','data':{'messages':repeated}})
        state=read(path)
        assert not state['groups'][GROUP]['possibly_incomplete']
        assert state['groups']['900002']['possibly_incomplete']
        assert state['groups']['900002']['covered_until']==enabled
        assert not pilot.syncing
        saved_count=len(state['pending'])
        # Replay on reconnect stays deduplicated, checkpoints never cover an unresolved gap.
        c.checkpoint()
        assert read(path)['groups']['900002']['covered_until']==enabled
        c.groups([{'group_id':GROUP},{'group_id':'900002'}])
        for _ in range(2):
            gid=c.active['gid']; records=batch if gid==GROUP else repeated
            c.response({'echo':sent[-1]['echo'],'status':'ok','data':{'messages':records[:99]}})
        assert len(read(path)['pending'])==saved_count
        c.groups([{'group_id':GROUP},{'group_id':'900002'},{'group_id':'900006'}],reconnect=False)
        assert c.active['gid']=='900006'
        c.response({'echo':sent[-1]['echo'],'status':'ok','data':{'messages':[]}})
        assert read(path)['group_count']==3
        pilot.receive(dict(msg(500,GROUP,'私聊哨兵'),message_type='private'))
        assert '私聊哨兵' not in path.read_text(encoding='utf-8')
        assert read(path)['cards'][original]==original_card
    print('PASS: migration, activation boundary, 2-group queue, same-name isolation, cross-link guard, restart dedup, manual state, history paging/gap, dynamic membership, private filtering')

if __name__=='__main__': main()
