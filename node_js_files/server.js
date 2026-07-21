// Entry point: wires up middleware, view engine, static assets, and routers.

const path = require('path');
const express = require('express');
const cors = require('cors');

const requestLogger = require('./middleware/logger');
const webRoutes = require('./routes/web');
const apiRoutes = require('./routes/api');

const app = express();
const PORT = process.env.PORT || 3000;

// View engine
app.set('view engine', 'ejs');
app.set('views', path.join(__dirname, 'views'));

// Global middleware
app.use(cors());
app.use(express.json());
app.use(express.urlencoded({ extended: true }));
app.use(express.static(path.join(__dirname, 'public')));
app.use(requestLogger);

// Routers
app.use('/', webRoutes);
app.use('/api', apiRoutes);

// Health check
app.get('/health', (req, res) => {
  res.json({ status: 'ok', notes: apiRoutes ? 'up' : 'up' });
});

// 404 handler
app.use((req, res) => {
  if (req.path.startsWith('/api')) {
    return res.status(404).json({ error: 'Route not found' });
  }
  res.status(404).render('error', { title: 'Not Found', message: 'Page not found' });
});

// Error handler
// eslint-disable-next-line no-unused-vars
app.use((err, req, res, next) => {
  console.error(err.stack);
  res.status(500).json({ error: 'Internal server error' });
});

app.listen(PORT, () => {
  console.log(`Notes app running on http://localhost:${PORT}`);
});
