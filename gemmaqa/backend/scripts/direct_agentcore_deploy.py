"""
Direct AgentCore deployment using AWS CDK/CloudFormation
This script packages and deploys the Strands agent to AgentCore Runtime
"""
import os
import sys
import boto3
import zipfile
from pathlib import Path

def create_deployment_package():
    """Create a ZIP package of the agent code"""
    print("Creating deployment package...")
    
    backend_dir = Path(__file__).parent.parent
    zip_path = backend_dir / "agent_package.zip"
    
    files_to_include = [
        "agentcore_app.py",
        "app/strands_agent/http_tools.py",
        "app/strands_agent/agentcore_builder.py",
        "app/strands_agent/context.py",
        "app/strands_agent/operations.py",
        "app/config.py",
        "requirements-agentcore.txt",
        "requirements-strands.txt",
    ]
    
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for file_path in files_to_include:
            full_path = backend_dir / file_path
            if full_path.exists():
                zipf.write(full_path, file_path)
                print(f"  Added: {file_path}")
    
    print(f"[OK] Package created: {zip_path}")
    return zip_path

def upload_to_s3(zip_path, bucket_name):
    """Upload the package to S3"""
    print(f"\nUploading to S3 bucket: {bucket_name}...")
    
    s3 = boto3.client('s3', region_name='us-east-1')
    key = "gemmaqa-agent/agent_package.zip"
    
    try:
        s3.upload_file(str(zip_path), bucket_name, key)
        print(f"[OK] Uploaded to s3://{bucket_name}/{key}")
        return f"s3://{bucket_name}/{key}"
    except Exception as e:
        print(f"[ERROR] Upload failed: {e}")
        return None

def main():
    print("========================================")
    print("GemmaQA AgentCore Direct Deployment")
    print("========================================\n")
    
    # Check AWS credentials
    try:
        sts = boto3.client('sts', region_name='us-east-1')
        identity = sts.get_caller_identity()
        print(f"AWS Account: {identity['Account']}")
        print(f"User: {identity['Arn']}\n")
    except Exception as e:
        print(f"[ERROR] AWS credentials not configured: {e}")
        return 1
    
    # Get backend URL
    backend_url = os.getenv('GEMMAQA_BACKEND_URL')
    if not backend_url:
        backend_url = input("Enter backend URL (e.g., https://your-ngrok-url): ").strip()
    
    print(f"Backend URL: {backend_url}\n")
    
    # Create package
    zip_path = create_deployment_package()
    
    print("\n========================================")
    print("Next Steps (Manual)")
    print("========================================\n")
    print("The code package is ready at:")
    print(f"  {zip_path}\n")
    print("To deploy to AgentCore, you need to:")
    print("1. Install uv: https://github.com/astral-sh/uv#installation")
    print("2. Run: agentcore create (follow prompts)")
    print("3. Copy your code files to the created project")
    print("4. Run: agentcore deploy\n")
    print("OR")
    print("\nUse AWS CDK/CloudFormation to deploy directly")
    print("See: https://docs.aws.amazon.com/bedrock/latest/userguide/agentcore.html")
    
    return 0

if __name__ == "__main__":
    sys.exit(main())
