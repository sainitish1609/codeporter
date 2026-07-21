// Per-route auth middleware: guards mutating API endpoints with an API key header.

function requireApiKey(req, res, next) {
  const apiKey = req.header('x-api-key');
  if (apiKey !== 'secret123') {
    return res.status(401).json({ error: 'Unauthorized' });
  }
  next();
}

module.exports = requireApiKey;
