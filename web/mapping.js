'use strict';
(function(root) {
  function selectionConflicts(nodes, choices, selected) {
    const targets = new Map(), conflicts = [];
    for (const row of nodes) {
      if (!selected.has(row.node_id)) continue;
      const candidate = row.candidates.find(c => c.type + ':' + c.id === choices.get(row.node_id));
      if (!candidate) {
        conflicts.push({code:'candidate_missing', node_ids:[row.node_id], message:(row.label || 'Node #' + row.node_id) + ': Bitte einen aktuellen Kandidaten auswählen.'});
        continue;
      }
      const key = candidate.type + ':' + candidate.id;
      if (!targets.has(key)) targets.set(key, {candidate, rows:[]});
      targets.get(key).rows.push(row);
    }
    for (const {candidate, rows} of targets.values()) {
      if (rows.length < 2) continue;
      const hosts = rows.map(row => (row.label || 'Ohne Namen') + ' (#' + row.node_id + ')').join(', ');
      conflicts.push({code:'duplicate_target', node_ids:rows.map(row => row.node_id),
        message:candidate.name + ' (' + candidate.type + ' #' + candidate.id + ') ist mehrfach ausgewählt: ' + hosts + '. Nur einen Node auswählen oder unterschiedliche Ziele wählen.'});
    }
    return conflicts;
  }
  const api = {selectionConflicts};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.InventoryMapping = api;
})(globalThis);
