// Simple in-memory "database" for notes.
// No external DB so the app boots with only `npm install`.

let notes = [
  { id: 1, title: 'Welcome', body: 'This is your first note.', pinned: true },
  { id: 2, title: 'Groceries', body: 'Milk, eggs, bread.', pinned: false },
  { id: 3, title: 'Ideas', body: 'Convert this Express app to Flask.', pinned: false },
];
let nextId = 4;

function list({ pinned } = {}) {
  if (pinned === undefined) return notes;
  return notes.filter((n) => n.pinned === pinned);
}

function get(id) {
  return notes.find((n) => n.id === id);
}

function create({ title, body, pinned = false }) {
  const note = { id: nextId++, title, body, pinned: Boolean(pinned) };
  notes.push(note);
  return note;
}

function update(id, fields) {
  const note = get(id);
  if (!note) return undefined;
  if (fields.title !== undefined) note.title = fields.title;
  if (fields.body !== undefined) note.body = fields.body;
  if (fields.pinned !== undefined) note.pinned = Boolean(fields.pinned);
  return note;
}

function remove(id) {
  const index = notes.findIndex((n) => n.id === id);
  if (index === -1) return false;
  notes.splice(index, 1);
  return true;
}

module.exports = { list, get, create, update, remove };
