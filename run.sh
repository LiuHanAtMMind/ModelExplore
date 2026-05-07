#!/bin/bash

BIN=./model_chat/install/jetson/bin/model_chat
MODEL_DIR=./models

# Scan for .gguf files
mapfile -t models < <(find "$MODEL_DIR" -name "*.gguf" -type f | sort)

if [ ${#models[@]} -eq 0 ]; then
    echo "No .gguf models found in $MODEL_DIR"
    exit 1
fi

echo "Available models:"
for i in "${!models[@]}"; do
    echo "  $((i+1)). ${models[$i]}"
done

echo ""
read -p "Select model [1-${#models[@]}]: " choice

if ! [[ "$choice" =~ ^[0-9]+$ ]] || [ "$choice" -lt 1 ] || [ "$choice" -gt ${#models[@]} ]; then
    echo "Invalid selection"
    exit 1
fi

MODEL="${models[$((choice-1))]}"
echo "Loading: $MODEL"
echo ""

$BIN -m "$MODEL" -n 8192 --ctx-size 16384 --use-direct-io