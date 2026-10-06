#!/bin/zsh
set -eu
print 'Save your Fish Audio API key in macOS Keychain.'
print 'Paste the key at the hidden Password prompt below and press Enter.'
print 'The key will not be displayed on screen.'
/usr/bin/security add-generic-password -U -a default -s 'ai-voice/FISH_API_KEY' -w
print '\nKey saved. You can close this window.'
read -r '?Press Enter to close…'
