const {test} = require('node:test');
const assert = require('node:assert/strict');
const {selectionConflicts} = require('../web/mapping.js');

function row(node, id=123, type='devices') {
  return {node_id:node, label:'host-'+node, candidates:[{type, id, name:'asset-'+id}]};
}
const choices = rows => new Map(rows.map(r=>[r.node_id, r.candidates[0].type+':'+r.candidates[0].id]));

test('duplicate choices identify both nodes, including rows outside current table filter', () => {
  const rows=[row('42'),row('43'),row('44',124)];
  const conflicts=selectionConflicts(rows,choices(rows),new Set(['42','43','44']));
  assert.equal(conflicts.length,1);
  assert.deepEqual(conflicts[0].node_ids,['42','43']);
  for(const text of ['host-42','host-43','devices #123'])assert.ok(conflicts[0].message.includes(text));
});
test('removing one duplicate selection resolves the conflict', () => {
  const rows=[row('42'),row('43')];
  assert.deepEqual(selectionConflicts(rows,choices(rows),new Set(['42'])),[]);
});
test('choosing a different target resolves the conflict', () => {
  const rows=[row('42'),row('43',124)];
  assert.deepEqual(selectionConflicts(rows,choices(rows),new Set(['42','43'])),[]);
});
test('Device and VM with the same numeric ID are distinct targets', () => {
  const rows=[row('42'),row('43',123,'virtual-machines')];
  assert.deepEqual(selectionConflicts(rows,choices(rows),new Set(['42','43'])),[]);
});
test('stale candidate choice blocks saving rather than dropping a selected host', () => {
  const rows=[row('42')];
  const conflicts=selectionConflicts(rows,new Map([['42','devices:999']]),new Set(['42']));
  assert.equal(conflicts[0].code,'candidate_missing');
  assert.deepEqual(conflicts[0].node_ids,['42']);
});
