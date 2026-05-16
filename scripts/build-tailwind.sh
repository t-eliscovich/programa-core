#!/usr/bin/env bash
# Rebuild static/tailwind.css from templates.  Run after adding classes the
# current CSS doesn't cover (pages will look "unstyled" until you rebuild).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/.tw-build" 2>/dev/null || {
  mkdir -p "$ROOT/.tw-build" && cd "$ROOT/.tw-build"
  cat > package.json << 'JSON'
{ "name": "tw", "version": "1.0.0", "private": true }
JSON
  npm i -D tailwindcss@3.4.4 --silent
}

cat > tailwind.config.js << 'JS'
module.exports = {
  darkMode: 'class',
  content: [
    '../templates/*.html',
    '../templates/**/*.html',
    '../modules/**/templates/**/*.html',
  ],
  // Safelist: clases que aparecen sólo en strings dinámicos
  // (macros wizard_* en templates/_ui.html) y que el JIT
  // de Tailwind no detecta como literales.
  safelist: [
    // Botones primarios del wizard_footer
    'bg-emerald-600', 'hover:bg-emerald-700', 'focus:ring-emerald-500',
    'bg-sky-600',     'hover:bg-sky-700',     'focus:ring-sky-500',
    'bg-indigo-600',  'hover:bg-indigo-700',  'focus:ring-indigo-500',
    'bg-rose-600',    'hover:bg-rose-700',    'focus:ring-rose-500',
    'bg-amber-600',   'hover:bg-amber-700',   'focus:ring-amber-500',
    'bg-violet-600',  'hover:bg-violet-700',  'focus:ring-violet-500',
    // Icon backgrounds del wizard_hero
    'bg-emerald-100', 'text-emerald-700', 'dark:bg-emerald-900/40', 'dark:text-emerald-300',
    'bg-sky-100',     'text-sky-700',     'dark:bg-sky-900/40',     'dark:text-sky-300',
    'bg-indigo-100',  'text-indigo-700',  'dark:bg-indigo-900/40',  'dark:text-indigo-300',
    'bg-rose-100',    'text-rose-700',    'dark:bg-rose-900/40',    'dark:text-rose-300',
    'bg-amber-100',   'text-amber-700',   'dark:bg-amber-900/40',   'dark:text-amber-300',
    'bg-violet-100',  'text-violet-700',  'dark:bg-violet-900/40',  'dark:text-violet-300',
    // Banners del wizard_info
    'bg-emerald-50',  'border-emerald-200', 'text-emerald-900', 'dark:bg-emerald-900/20', 'dark:border-emerald-800', 'dark:text-emerald-200',
    'bg-sky-50',      'border-sky-200',     'text-sky-900',     'dark:bg-sky-900/20',     'dark:border-sky-800',     'dark:text-sky-200',
    'bg-indigo-50',   'border-indigo-200',  'text-indigo-900',  'dark:bg-indigo-900/20',  'dark:border-indigo-800',  'dark:text-indigo-200',
    'bg-rose-50',     'border-rose-200',    'text-rose-900',    'dark:bg-rose-900/20',    'dark:border-rose-800',    'dark:text-rose-200',
    'bg-amber-50',    'border-amber-200',   'text-amber-900',   'dark:bg-amber-900/20',   'dark:border-amber-800',   'dark:text-amber-200',
    'bg-violet-50',   'border-violet-200',  'text-violet-900',  'dark:bg-violet-900/20',  'dark:border-violet-800',  'dark:text-violet-200',
    // KPI card tonos (kpi_card macro en _ui.html)
    'text-slate-500', 'text-slate-900', 'dark:text-slate-100',
    'text-emerald-300', 'text-emerald-700/80', 'dark:text-emerald-300/80', 'dark:text-emerald-100',
    'text-rose-300',    'text-rose-700/80',    'dark:text-rose-300/80',    'dark:text-rose-100',
    'text-amber-300',   'text-amber-700/80',   'dark:text-amber-300/80',   'dark:text-amber-100',
    'text-sky-300',     'text-sky-700/80',     'dark:text-sky-300/80',     'dark:text-sky-100',
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
    },
  },
}
JS

cat > input.css << 'CSS'
@tailwind base;
@tailwind components;
@tailwind utilities;
CSS

./node_modules/.bin/tailwindcss \
  -c tailwind.config.js \
  -i input.css \
  -o "$ROOT/static/tailwind.css" \
  --minify

echo "Built $ROOT/static/tailwind.css"
ls -lh "$ROOT/static/tailwind.css"
