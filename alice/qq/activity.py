"""One person's attention across text turns and the finite call experiment."""
import asyncio,os,time,uuid

def claim(store,owner,now=None):
    now=time.time() if now is None else now
    store.db.execute('CREATE TABLE IF NOT EXISTS qq_activity(slot INTEGER PRIMARY KEY,owner TEXT,until REAL)')
    with store.db:
        store.db.execute('DELETE FROM qq_activity WHERE until<=?',(now,))
        cur=store.db.execute('INSERT OR IGNORE INTO qq_activity VALUES(1,?,?)',(owner,now+60))
    return bool(cur.rowcount)

def release(store,owner):
    with store.db:store.db.execute('DELETE FROM qq_activity WHERE slot=1 AND owner=?',(owner,))

async def acquire(store,label,timeout=195):
    owner=str(os.getpid())+':'+label+':'+uuid.uuid4().hex
    deadline=time.monotonic()+timeout
    while not claim(store,owner):
        if time.monotonic()>=deadline:raise RuntimeError('qq_attention_busy')
        await asyncio.sleep(.25)
    class Lease:
        async def renew(self):
            while True:
                await asyncio.sleep(15)
                with store.db:
                    cur=store.db.execute('UPDATE qq_activity SET until=? WHERE slot=1 AND owner=?',(time.time()+60,owner))
                if not cur.rowcount:raise RuntimeError('qq_attention_lease_lost')
        async def close(self):
            self.task.cancel();await asyncio.gather(self.task,return_exceptions=True)
            release(store,owner)
    lease=Lease();lease.owner=owner;lease.task=asyncio.create_task(lease.renew())
    return lease
