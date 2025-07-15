#!/bin/bash
# Simple wrapper for the Python build script

echo "🚀 PPA Contatto Release Script"
echo ""

# Check if Python is available
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 is required but not installed."
    exit 1
fi

# Check if we're in the right directory
if [ ! -f "custom_components/ppa_contatto/manifest.json" ]; then
    echo "❌ Please run this script from the project root directory"
    exit 1
fi

# Run the Python script
python3 build_release.py
