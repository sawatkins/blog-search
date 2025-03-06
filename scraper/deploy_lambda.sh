#!/bin/bash

# Configuration - change these values
FUNCTION_NAME="blogsearch_scrape_url"
LAMBDA_DIR="./lambda"
REGION="us-west-2"

echo "Packaging Lambda function: $FUNCTION_NAME"
rm -rf /tmp/lambda-package
mkdir -p /tmp/lambda-package

cp -r $LAMBDA_DIR/* /tmp/lambda-package/

cd /tmp/lambda-package
pip install -r requirements.txt -t .

zip -r function.zip .

aws lambda update-function-code \
  --function-name $FUNCTION_NAME \
  --region $REGION \
  --zip-file fileb://function.zip

cd -
rm -rf /tmp/lambda-package
echo "Lambda function updated successfully!"
