#!/bin/sh
sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$(dirname "$0")/../src/ai_voice/__init__.py"
