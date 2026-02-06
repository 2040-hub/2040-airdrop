#!/bin/bash

git pull
git add -A .
git commit -S -m "$1"
git push origin main