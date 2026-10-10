#!/usr/bin/env bash
# Create an AWS EC2 node for the CloudMorph node pool (what we ran on 10/10).
#   AWS_REGION=ap-northeast-2 CONTROL_IP=34.64.253.41 CONTROL_PUBKEY="ssh-ed25519 ..." deploy/nodes/aws-node.sh [name]
# Then add to backend/nodes.json on the control server:
#   {"name":"aws-node-1","provider":"aws","host":"<public ip>","user":"ubuntu","key":"~/.ssh/id_ed25519","arch":"amd64","ports":[8000,8100]}
set -euo pipefail
NAME="${1:-cloudmorph-aws-node-1}"; TYPE="${INSTANCE_TYPE:-t3.small}"
: "${CONTROL_IP:?}"; : "${CONTROL_PUBKEY:?}"
MYIP=$(curl -s https://checkip.amazonaws.com | tr -d '\n')
VPC=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
aws ec2 describe-key-pairs --key-names cloudmorph-laptop >/dev/null 2>&1 || \
  aws ec2 import-key-pair --key-name cloudmorph-laptop --public-key-material "fileb://$HOME/.ssh/id_ed25519.pub" >/dev/null
SG=$(aws ec2 describe-security-groups --filters Name=group-name,Values=cloudmorph-node Name=vpc-id,Values="$VPC" --query 'SecurityGroups[0].GroupId' --output text)
if [[ "$SG" == "None" ]]; then
  SG=$(aws ec2 create-security-group --group-name cloudmorph-node --description "CloudMorph node pool" --vpc-id "$VPC" --query GroupId --output text)
  aws ec2 authorize-security-group-ingress --group-id "$SG" --ip-permissions \
    "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=$CONTROL_IP/32},{CidrIp=$MYIP/32}]" \
    "IpProtocol=tcp,FromPort=8000,ToPort=8100,IpRanges=[{CidrIp=0.0.0.0/0}]" >/dev/null
fi
ARCH=$([[ "$TYPE" == *g.* ]] && echo arm64 || echo amd64)
AMI=$(aws ssm get-parameter --name "/aws/service/canonical/ubuntu/server/24.04/stable/current/$ARCH/hvm/ebs-gp3/ami-id" --query Parameter.Value --output text)
USERDATA=$(printf '#!/bin/bash\nset -e\napt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io\nusermod -aG docker ubuntu\nsystemctl enable --now docker\necho "%s" >> /home/ubuntu/.ssh/authorized_keys\ntouch /var/run/cloudmorph-ready\n' "$CONTROL_PUBKEY")
IID=$(aws ec2 run-instances --image-id "$AMI" --instance-type "$TYPE" --key-name cloudmorph-laptop --security-group-ids "$SG" \
  --user-data "$USERDATA" --block-device-mappings 'DeviceName=/dev/sda1,Ebs={VolumeSize=20,VolumeType=gp3}' \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME},{Key=project,Value=cloudmorph}]" --query 'Instances[0].InstanceId' --output text)
aws ec2 wait instance-running --instance-ids "$IID"
aws ec2 describe-instances --instance-ids "$IID" --query 'Reservations[0].Instances[0].[InstanceId,InstanceType,PublicIpAddress]' --output text
echo "arch=$ARCH  (use this in nodes.json)"
