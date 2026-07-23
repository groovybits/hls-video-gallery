# Security policy

## Reporting a vulnerability

Do not open a public issue containing credentials, share tokens, private filenames,
or a reproducible exploit against a live gallery. Contact the repository owner
privately and include the affected version and a minimal reproduction.

## Deployment expectations

- Use HTTPS. The shipped Apache rules redirect HTTP to HTTPS.
- Keep `config/gallery.json`, `config/users.txt`, and `config/bunny.env` out of Git.
- Keep the generated private directory outside the document root.
- Use long, unique passwords and rotate them if a configuration file is exposed.
- Treat guest share URLs as bearer secrets. Anyone holding one can watch that one
  cached version until its source is replaced or removed.
- Review the Apache virtual host to ensure `AllowOverride All` (or equivalent
  explicit rules) applies to the gallery directory.
- Keep the OS, Apache, PHP, FFmpeg, and Python packages updated.

This project does not provide DRM. Authentication and signed URLs control access;
an authorized viewer can still record or redistribute media.
