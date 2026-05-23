#!/bin/bash
cd "$(dirname "$0")"
source venv/bin/activate
python migrate_to_supabase.py
echo ""
echo "Presiona Enter para cerrar..."
read
