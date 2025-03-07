import asyncio
import json
import os
import subprocess
import uuid
import shutil
import yaml
import argparse

from apicurioregistrysdk.client.models.version_state import VersionState
from apicurioregistrysdk.client.registry_client import RegistryClient
from confluent_kafka import Consumer, KafkaError
from httpx import AsyncClient
from kiota_abstractions.authentication import AnonymousAuthenticationProvider
from kiota_http.httpx_request_adapter import HttpxRequestAdapter

GROUP_ID = uuid.uuid4()

def create_consumer(kafka_bootstrap_server, kafka_topic):
    """
    Create and return a Kafka consumer configured for the given topic and server.
    """
    consumer = Consumer({
        'bootstrap.servers': kafka_bootstrap_server,
        'group.id': GROUP_ID,
        'auto.offset.reset': 'earliest',
        'security.protocol': 'SSL',
        'enable.ssl.certificate.verification': 'false',
    })
    consumer.subscribe([kafka_topic])
    return consumer

def consume_messages_in_batches(consumer, apicurio_registry_url, batch_size=10, timeout=5, idle_timeout=10):
    """
    Consume Kafka messages in batches. If no messages are received within the idle_timeout, invoke the Kuadrant CLI.
    """
    idle_time = 0  # Tracks the time spent idle
    try:
        while True:
            messages = consumer.consume(batch_size, timeout=timeout)

            if messages:
                idle_time = 0
                process_messages(messages, apicurio_registry_url)
            else:
                idle_time += timeout
                if idle_time >= idle_timeout:
                    print("No messages left in Kafka. Pushing files to git...")
                    commit_and_push_to_git()
                    break
    finally:
        consumer.close()

def process_messages(messages, apicurio_registry_url):
    """
    Process a batch of messages.
    """
    for msg in messages:
        if msg.error():
            if msg.error().code() == KafkaError._PARTITION_EOF:
                continue
            else:
                print(f"Error occurred: {msg.error()}")
                continue

        try:
            process_message(msg, apicurio_registry_url)
        except json.JSONDecodeError as e:
            print(f"Error decoding message: {e}")

def process_message(msg, apicurio_registry_url):
    """
    Process the Kafka message to extract artifactId, version, and group_id (if present).
    """
    try:
        msg_value = json.loads(msg.value().decode('utf-8'))
        payload = json.loads(msg_value['payload'])

        artifact_id = payload.get('artifactId')
        version = payload.get('version')

        event_type = payload.get('eventType')
        group_id = payload.get('groupId', 'default')

        if event_type == "ARTIFACT_VERSION_CREATED":
            print(f"Artifact version created event - {event_type}: Artifact ID: {artifact_id}, Version: {version}")
            asyncio.run(get_artifact_content(group_id, artifact_id, version, apicurio_registry_url))
        elif event_type == "ARTIFACT_VERSION_STATE_CHANGED":
            new_state = payload.get('newState')
            if new_state == 'ENABLED':
                print(f"Artifact version enabled event - {event_type}: Artifact ID: {artifact_id}, Version: {version}")
                asyncio.run(get_artifact_content(group_id, artifact_id, version, apicurio_registry_url))
            else:
                print(f"Skipping artifact {artifact_id} with newState: {new_state}")
        elif event_type == "ARTIFACT_DELETED":
            print(f"Artifact deleted event - {event_type}: Artifact ID: {artifact_id}")
            delete_artifact_directory(group_id, artifact_id)
        elif event_type == "ARTIFACT_VERSION_DELETED":
            print(f"Artifact version deleted event - {event_type}: Artifact ID: {artifact_id}, Version: {version}")
            delete_version(group_id, artifact_id, version)
        else:
            print(f"Other Event - {event_type}: Artifact ID: {artifact_id}, Version: {version}")

    except json.JSONDecodeError as e:
        print(f"Failed to decode JSON message: {e}")
    except KeyError as e:
        print(f"Missing expected key: {e}")

async def get_artifact_content(group_id, artifact_id, version, apicurio_registry_url):
    """
    Fetch the artifact content from Apicurio Registry using the artifact ID and version.
    """
    try:
        authentication_provider = AnonymousAuthenticationProvider()
        client = AsyncClient(verify=False)
        request_adapter = HttpxRequestAdapter(authentication_provider=authentication_provider, http_client=client)
        request_adapter.base_url = apicurio_registry_url
        client = RegistryClient(request_adapter)

        artifact_content = await client.groups.by_group_id(group_id).artifacts.by_artifact_id(
            artifact_id).versions.by_version_expression(version).content.get()

        state = await client.groups.by_group_id(group_id).artifacts.by_artifact_id(artifact_id).versions.by_version_expression(version).state.get()

        if state.state == VersionState.ENABLED:
            invoke_kuadrant_cli(group_id, artifact_id, version, artifact_content)
    except Exception as e:
        print(f"Failed to retrieve artifact content for {artifact_id} version {version}: {e}")
        return None

def delete_artifact_directory(group_id, artifact_id):
    """
    Deletes the entire directory for a specific group_id and artifact_id,
    including all version subdirectories.
    """
    folder_path = os.path.join(
        os.path.dirname(os.getcwd()),
        'api-resources/api-resources',
        group_id,
        artifact_id
    )

    if os.path.exists(folder_path):
        shutil.rmtree(folder_path)
        print(f"Deleted artifact directory with all versions: {folder_path}")
    else:
        print(f"Artifact directory does not exist: {folder_path}")

def delete_version(group_id, artifact_id, version):
    """
    Deletes the directory corresponding to a specific group_id, artifact_id, and version.
    """
    folder_path = os.path.join(
        os.path.dirname(os.getcwd()),
        'api-resources/api-resources',
        group_id,
        artifact_id,
        version
    )

    if os.path.exists(folder_path):
        shutil.rmtree(folder_path)  # Recursively deletes the directory
        print(f"Deleted directory: {folder_path}")
    else:
        print(f"Directory does not exist: {folder_path}")

def invoke_kuadrant_cli(group_id, artifact_id, version, openapi_content):
    """
    Invokes the kuadrantctl CLI for the given coordinates and .
    """
    invoke_kuadrant_command(group_id, artifact_id, version, ['kuadrantctl', 'generate', 'gatewayapi', 'httproute', '--oas', '-'], openapi_content, 'httproute')
    invoke_kuadrant_command(group_id, artifact_id, version, ['kuadrantctl', 'generate', 'kuadrant', 'authpolicy', '--oas', '-'], openapi_content, 'authpolicy')
    invoke_kuadrant_command(group_id, artifact_id, version, ['kuadrantctl', 'generate', 'kuadrant', 'ratelimitpolicy', '--oas', '-'], openapi_content, 'ratelimiting_policy')

def invoke_kuadrant_command(group_id, artifact_id, version, args, openapi_content, filename):
    # Define the path to store the generated Kuadrant resources

    # Define the path based on group_id, artifact_id, and version
    folder_path = os.path.join(
        os.path.dirname(os.getcwd()),
        'api-resources/api-resources',
        group_id,
        artifact_id,
        version
    )

    os.makedirs(folder_path, exist_ok=True)

    # Define the output filename for the generated Kuadrant resource
    filename = f"{group_id}_{artifact_id}_v{version}_{filename}.yaml"
    file_path = os.path.join(folder_path, filename)

    try:
        if isinstance(openapi_content, bytes):
            openapi_content = openapi_content.decode("utf-8")

        # Invoke kuadrantctl to generate resources from OpenAPI content
        process = subprocess.run(
            args,
            input=openapi_content,  # Pass the OpenAPI content as input
            text=True,  # Treat input as a string
            capture_output=True,
            check=True
        )

        data = yaml.safe_load(process.stdout)

        #Remove the status part, otherwise the sync will fail
        data.pop('status', None)

        # Write the generated output to the file
        with open(file_path, 'w') as file:
            yaml.dump(data, file, default_flow_style=False)
        print(f"Kuadrant resource generated and saved to {file_path}")

    except subprocess.CalledProcessError as e:
        print(f"Error generating Kuadrant resources:\n{e.stderr}")

def commit_and_push_to_git():
    """
    Commit and push all changes in the api-resources/api-resources directory to the Git repository.
    """
    api_resources_path = os.path.join(os.path.dirname(os.getcwd()), 'api-resources/api-resources')

    try:
        subprocess.run(['git', '-C', api_resources_path, 'add', '.'], check=True)

        commit_message = "Update Kuadrant resources"
        subprocess.run(['git', '-C', api_resources_path, 'commit', '-m', commit_message], check=True)

        subprocess.run(['git', '-C', api_resources_path, 'push'], check=True)

        print("Successfully committed and pushed changes to the Git repository.")
    except subprocess.CalledProcessError as e:
        print(f"Error committing and pushing to Git:\n{e.stderr}")

def main(kafka_bootstrap_server, apicurio_registry_url, kafka_topic):
    """
    Main function to set up Kafka consumer and process messages in a loop.
    """

    print(f"Kafka Bootstrap Server: {kafka_bootstrap_server}")
    print(f"Apicurio Registry URL: {apicurio_registry_url}")
    print(f"Kafka Events Topic: {kafka_topic}")

    consumer = create_consumer(kafka_bootstrap_server, kafka_topic)
    consume_messages_in_batches(consumer, apicurio_registry_url, batch_size=10, timeout=5)

# Entry point of the script
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Process Kafka messages and interact with Apicurio Registry")
    parser.add_argument('--kafka-bootstrap-server', required=True, help="Kafka bootstrap server (e.g., localhost:9092)")
    parser.add_argument('--apicurio-registry-url', required=True,
                        help="URL for the Apicurio Registry (e.g., http://localhost:8080/apis/registry/v3)")
    parser.add_argument('--kafka-topic', required=True,
                        help="Kafka Topic source for Apicurio Registry events")

    args = parser.parse_args()
    main(args.kafka_bootstrap_server, args.apicurio_registry_url, args.kafka_topic)
