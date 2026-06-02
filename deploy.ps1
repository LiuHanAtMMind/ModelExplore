# Mirror the workspace so renamed or deleted source files do not linger on Jetson.
& "C:\Program Files\Git\bin\bash.exe" -c "rsync -avz --delete --filter=':- deploy_ignore' -e ssh ./ ubuntu@192.168.23.250:/home/ubuntu/liuhan/deploy/"
ssh ubuntu@192.168.23.250 "cd /home/ubuntu/liuhan/deploy/model_chat && bash ./build.sh all && chmod +x ../run_chat.sh"
