// JSON REST API for notes. Mounted at "/api" in server.js.

const express = require('express');
const store = require('../data/store');
const requireApiKey = require('../middleware/auth');

const router = express.Router();

// GET /api/notes - list, optional ?pinned=true|false filter
router.get('/notes', (req, res) => {
  const { pinned } = req.query;
  let result;
  if (pinned === undefined) {
    result = store.list();
  } else {
    result = store.list({ pinned: pinned === 'true' });
  }
  res.json(result);
});

// GET /api/notes/:id - single note
router.get('/notes/:id', (req, res) => {
  const id = parseInt(req.params.id, 10);
  const note = store.get(id);
  if (!note) {
    return res.status(404).json({ error: 'Note not found' });
  }
  res.json(note);
});

// POST /api/notes - create (protected)
router.post('/notes', requireApiKey, (req, res) => {
  const { title, body, pinned } = req.body;
  if (!title || typeof title !== 'string') {
    return res.status(400).json({ error: 'Title is required and must be a string' });
  }
  const note = store.create({ title, body: body || '', pinned: Boolean(pinned) });
  res.status(201).json(note);
});

// PUT /api/notes/:id - update (protected)
router.put('/notes/:id', requireApiKey, (req, res) => {
  const id = parseInt(req.params.id, 10);
  const note = store.update(id, req.body);
  if (!note) {
    return res.status(404).json({ error: 'Note not found' });
  }
  res.json(note);
});

// DELETE /api/notes/:id - delete (protected)
router.delete('/notes/:id', requireApiKey, (req, res) => {
  const id = parseInt(req.params.id, 10);
  const ok = store.remove(id);
  if (!ok) {
    return res.status(404).json({ error: 'Note not found' });
  }
  res.status(204).send();
});

module.exports = router;
