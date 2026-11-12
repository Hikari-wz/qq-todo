"""Offline share placeholders and bounded history coverage regression."""
import json
import tempfile
from pathlib import Path
from napcat_pilot import Pilot, accept_history
from todo_server import LanHandler
from trial import read, save, set_manual_status
from verify_napcat import event, PROFILE, GROUP

def main():
    with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent.parent / "work") as tmp:
        path=Path(tmp)/'state.json'
        pilot=Pilot(path,GROUP,PROFILE,9931)
        pilot.extract=lambda ctx: (_ for _ in ()).throw(AssertionError('share must stay local'))
        first=event(71,'')
        first['message']=[{'type':'json','data':{'data':'{"private":"LOCAL_ONLY_SENTINEL"}'}}]
        pilot.receive(first); assert pilot.process_next()
        state=read(path); key=next(iter(state['cards']))
        assert state['cards'][key]['decision']=='review'
        assert state['processed'][next(iter(state['processed']))]['message']['local_shares']==first['message']
        set_manual_status(state,key,'done'); save(path,state)
        pilot=Pilot(path,GROUP,PROFILE,9931)
        pilot.receive(first); assert pilot.process_next() is None
        second=dict(first,message_id=72)
        pilot.receive(second); assert pilot.process_next()
        state=read(path); second_key=[k for k in state['cards'] if k!=key][0]
        set_manual_status(state,second_key,'dismissed'); save(path,state)
        pilot.receive(second)
        state=read(path)
        assert len(state['cards'])==2 and state['stats']['duplicate']==2
        assert state['cards'][key]['manual_status']=='done' and state['cards'][second_key]['manual_status']=='dismissed'
        public=LanHandler.cards(object.__new__(LanHandler),state)
        assert 'LOCAL_ONLY_SENTINEL' not in json.dumps(public)
        assert all(c['message_at'] and c['evidence'][0]['text'] == '[分享卡片]' for c in public)
        baseline=state['cards']
        # Other-group records only test coverage bookkeeping, never enqueue.
        records=[dict(event(i,'',group='900002'),time=1001+i) for i in range(100)]
        accept_history(pilot,records,1000)
        assert read(path)['history_check']['possibly_incomplete']
        records[0]['time']=1000
        accept_history(pilot,records,1000)
        assert not read(path)['history_check']['possibly_incomplete']
        accept_history(pilot,records[1:],1000)
        assert not read(path)['history_check']['possibly_incomplete']
        assert read(path)['cards']==baseline
    print('PASS: share local retention, per-message dedup/restart, done/ignored preservation, phone redaction, history boundaries')

if __name__=='__main__': main()

