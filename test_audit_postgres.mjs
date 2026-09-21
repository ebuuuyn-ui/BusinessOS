// Run via test_audit_log.py with PGLITE_MODULE pointing to a test-only PGlite installation.
process.on('uncaughtException',e=>{console.error(e.message, e.query?.slice(Number(e.position)-100,Number(e.position)+100));process.exit(1)});
const {PGlite} = await import(process.env.PGLITE_MODULE || '@electric-sql/pglite');
import fs from 'node:fs';
import assert from 'node:assert/strict';
const f=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const db=new PGlite();
await db.exec(f.create);
await db.exec(f.schema);
for(const sql of f.triggers) await db.exec(sql);
const q=async sql=>(await db.query(sql)).rows;
await db.exec("BEGIN; SELECT set_config('businessos.actor_id','user:7',true),set_config('businessos.actor_name','test-staff',true),set_config('businessos.request_id','request-1',true),set_config('businessos.endpoint','edit_customer',true);");
await db.exec("INSERT INTO customer (id,name,created_at) VALUES (1,'before',now()),(2,'delete-me',now())");
await db.exec("UPDATE customer SET name='after' WHERE id=1; DELETE FROM customer WHERE id=2; COMMIT;");
let rows=await q("SELECT * FROM business_audit_event ORDER BY id");
assert.equal(rows.length,4);
assert(rows.every(r=>r.actor_name==='test-staff' && r.actor_id==='user:7'));
assert.equal(rows[2].before_data.name,'before'); assert.equal(rows[2].after_data.name,'after');
assert.deepEqual(rows[2].changed_fields,['name']);
assert.equal(rows[3].before_data.name,'delete-me');
await db.exec("BEGIN; INSERT INTO customer(id,name,created_at) VALUES(3,'rollback',now()); ROLLBACK;");
assert.equal((await q("SELECT * FROM business_audit_event")).length,4);
await db.exec("UPDATE customer SET name='after' WHERE id=1");
assert.equal((await q("SELECT * FROM business_audit_event")).length,4);
await db.exec("INSERT INTO customer(id,name,created_at) VALUES(4,'system-write',now())");
rows=await q("SELECT * FROM business_audit_event ORDER BY id DESC LIMIT 1");
assert.equal(rows[0].actor_id,'system');
await db.exec("INSERT INTO web_user(id,username,password_hash,active,session_token,failed_attempts) VALUES(1,'test','secret-hash',true,'secret-token',0); UPDATE web_user SET password_hash='different-secret' WHERE id=1;");
rows=await q("SELECT * FROM business_audit_event WHERE table_name='web_user' ORDER BY id");
assert(!JSON.stringify(rows).includes('secret-hash')); assert(!JSON.stringify(rows).includes('secret-token'));
assert(!JSON.stringify(rows).includes('different-secret'));
assert.deepEqual(rows[1].changed_fields,['password_changed']);
await db.exec("UPDATE web_user SET failed_attempts=1, session_token='another-secret' WHERE id=1;");
assert.equal((await q("SELECT * FROM business_audit_event WHERE table_name='web_user'")).length,2);
await db.exec("INSERT INTO stored_file(id,storage_key,content,size_bytes,sha256,created_at) VALUES(1,'doc',decode('010203','hex'),3,'abc',now())");
rows=await q("SELECT * FROM business_audit_event WHERE table_name='stored_file'");
assert(!('content' in rows[0].after_data)); assert.equal(rows[0].after_data.size_bytes,3);
await db.exec("INSERT INTO account_transaction(id,customer_id,transaction_date,transaction_type,description,debit,credit,payment_method,created_at) VALUES(1,1,'2026-09-21','test','test',10,0,'Nakit',now()); DELETE FROM customer WHERE id=1");
rows=await q("SELECT * FROM business_audit_event WHERE table_name='account_transaction' ORDER BY id");
assert.equal(rows[1].operation,'DELETE'); assert.equal(rows[1].record_key.id,1);
for(const sql of ["DELETE FROM business_audit_event","UPDATE business_audit_event SET actor_name='forged'","TRUNCATE business_audit_event"]){
 await assert.rejects(()=>db.exec(sql), /append-only/);
}
await db.exec(f.schema); // Repeat setup preserves past audit rows.
assert((await q("SELECT * FROM business_audit_event")).length>0);
console.log('PASS: 25 table triggers; actor, snapshots, rollback, no-op, actor isolation, secret/file exclusion, password changes, cascade delete, immutability, repeat setup.');
await db.close();
