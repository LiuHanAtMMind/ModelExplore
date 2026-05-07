& "C:\Program Files\Git\bin\bash.exe" -c "rsync -avz --filter=':- deploy_ignore' -e ssh ./ ubuntu@192.168.23.250:/home/ubuntu/liuhan/deploy/"
ssh ubuntu@192.168.23.250 "cd /home/ubuntu/liuhan/deploy/model_chat && bash ./build.sh app && chmod +x ../run.sh"
