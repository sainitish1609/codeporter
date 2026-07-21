// Server-rendered pages (EJS views). Mounted at "/" in server.js.

const express = require('express');
const store = require('../data/store');

const router = express.Router();

// GET / - list all notes (pinned first)
router.get('/', (req, res) => {
  const notes = store.list();
  const sorted = [...notes].sort((a, b) => Number(b.pinned) - Number(a.pinned));
  res.render('index', { title: 'All Notes', notes: sorted });
});

// GET /notes/new - form to create a note
router.get('/notes/new', (req, res) => {
  res.render('new', { title: 'New Note' });
});

// GET /notes/:id - single note detail
router.get('/notes/:id', (req, res) => {
  const id = parseInt(req.params.id, 10);
  const note = store.get(id);
  if (!note) {
    return res.status(404).render('error', { title: 'Not Found', message: 'Note not found' });
  }
  res.render('detail', { title: note.title, note });
});

// POST /notes - create a note from the form, then redirect
router.post('/notes', (req, res) => {
  const { title, body, pinned } = req.body;
  if (!title || !title.trim()) {
    return res.status(400).render('new', { title: 'New Note', error: 'Title is required' });
  }
  const note = store.create({ title: title.trim(), body: (body || '').trim(), pinned: pinned === 'on' });
  res.redirect(`/notes/${note.id}`);
});

// POST /notes/:id/delete - delete a note, then redirect home
router.post('/notes/:id/delete', (req, res) => {
  const id = parseInt(req.params.id, 10);
  store.remove(id);
  res.redirect('/');
});

module.exports = router;
