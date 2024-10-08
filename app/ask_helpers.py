import json
import time
import logging
import requests
import os
from flask import session
from flask_wtf import FlaskForm
from wtforms import TextAreaField
from wtforms.validators import DataRequired
from .initialize import client, analytics, config
from openai import OpenAIError

logger = logging.getLogger(__name__)

class ChatForm(FlaskForm):
    question = TextAreaField('Question', validators=[DataRequired()])

def get_product_info(product_id, pre_shared_key, product_info_webhook_url):
    try:
        # Construct the URL with the correct scheme
        url = f"{product_info_webhook_url}/{product_id}?key={pre_shared_key}"
        logger.debug(f"Sending request to URL: {url}")

        # Change POST to GET
        response = requests.get(url, timeout=10)
        logger.debug(f"Received response from webhook: {response.status_code} - {response.content}")
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        logger.error(f"Error fetching product info for ID {product_id}: {str(e)}")
        return {"error": str(e)}
    
def get_user_info(wp_username, pre_shared_key, user_info_webhook_url):
    try:
        if not wp_username:
            logger.error("wp_username is not provided, cannot fetch user info.")
            return {"error": "Missing wp_username"}
        
        # Remove trailing slash from the webhook URL if present
        user_info_webhook_url = user_info_webhook_url.rstrip('/')
        
        # Construct the URL with the correct scheme
        url = f"{user_info_webhook_url}/{wp_username}?key={pre_shared_key}"
        logger.debug(f"Sending request to URL: {url}")

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'application/json',
        }

        response = requests.get(url, headers=headers, timeout=10)
        
        logger.debug(f"Received response from webhook: {response.status_code}")
        logger.debug(f"Response headers: {response.headers}")
        logger.debug(f"Response content: {response.content}")

        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        logger.error(f"Error fetching user info for ID {wp_username}: {str(e)}")
        if hasattr(e, 'response'):
            logger.error(f"Response status code: {e.response.status_code}")
            logger.error(f"Response content: {e.response.content}")
        return {"error": str(e)}
    
def create_or_get_thread(question):
    session_id = session.get('sid')
    session_info = session.get('client_session_info', {})
    pre_shared_key = config.get('pre_shared_key', '')

    # Check if there's an existing thread ID in the session
    thread_id = session.get('thread_id')

    message_content = f"User question: {question}\n\nSession info: {json.dumps(session_info)}\n\nPre-shared key: {pre_shared_key}"

    def create_new_thread():
        try:
            thread = client.beta.threads.create()
            new_thread_id = thread.id
            session['thread_id'] = new_thread_id
            logger.debug(f"Created new thread with ID: {new_thread_id}")
            return new_thread_id
        except Exception as e:
            logger.error(f"Error creating new thread: {str(e)}")
            raise

    try:
        if thread_id:
            try:
                # Try to add a message to the existing thread
                client.beta.threads.messages.create(
                    thread_id=thread_id,
                    role="user",
                    content=message_content
                )
                logger.debug(f"Added message to existing thread {thread_id}")
            except OpenAIError as e:
                if e.status_code == 404:
                    logger.warning(f"Thread {thread_id} not found. Creating a new thread.")
                    thread_id = create_new_thread()
                else:
                    raise
        else:
            thread_id = create_new_thread()

        # Add the message to the thread (in case a new thread was created)
        client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=message_content
        )
        logger.debug(f"Added message to thread {thread_id}")

        # Create a run for the thread
        run = client.beta.threads.runs.create(
            thread_id=thread_id,
            assistant_id=config['assistant_id']
        )
        logger.debug(f"Created run {run.id} for thread {thread_id}")

        return thread_id, run

    except Exception as e:
        logger.error(f"Error in create_or_get_thread: {str(e)}")
        # If any error occurs, create a new thread as a fallback
        thread_id = create_new_thread()
        
        # Add the message to the new thread
        client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=message_content
        )
        logger.debug(f"Added message to new fallback thread {thread_id}")

        # Create a run for the new thread
        run = client.beta.threads.runs.create(
            thread_id=thread_id,
            assistant_id=config['assistant_id']
        )
        logger.debug(f"Created run {run.id} for new fallback thread {thread_id}")

        return thread_id, run

def generate_responses(thread_id, run):
    session_id = session.get('sid', 'unknown')
    session_info = session.get('client_session_info', {})
    logger.debug(f"Session ID in generate_responses: {session_id}")
    logger.debug(f"Session Info in generate_responses: {session_info}")

    # Make sure we log wp_username here
    if session_info.get('wp_username') != 'a8d6e69f_admin':
        logger.error(f"wp_username mismatch in session_info. Expected 'a8d6e69f_admin', found: {session_info.get('wp_username')}")

    start_time = time.time()
    timeout = 60
    max_retries = 30

    for attempt in range(max_retries):
        if time.time() - start_time > timeout:
            yield f"data: {json.dumps({'error': 'Request timed out'})}\n\n"
            break

        try:
            run_status = client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)

            if run_status.status == 'completed':
                messages = client.beta.threads.messages.list(thread_id=thread_id, limit=1)
                for message in messages.data:
                    if message.role == "assistant":
                        content = message.content[0].text.value
                        formatted_content = format_response(content)

                        # Track bot response
                        analytics.track(session_id, 'Bot Response Sent', {
                            'response': formatted_content,
                            'session_info': session_info
                        })

                        yield f"data: {formatted_content}\n\n"
                yield "event: DONE\ndata: [DONE]\n\n"
                break

            elif run_status.status in ['failed', 'cancelled', 'expired']:
                yield f"data: {json.dumps({'error': f'Run {run_status.status}'})}\n\n"
                break

            elif run_status.status == 'requires_action':
                # Add logs before calling handle_required_action
                logger.debug(f"Handling required action with session info: {session_info}")
                
                if handle_required_action(run_status, thread_id):
                    time.sleep(2)
                    continue
                else:
                    yield f"data: {json.dumps({'error': 'Unable to handle required action'})}\n\n"
                    break
            else:
                time.sleep(2)

        except Exception as e:
            yield f"data: {json.dumps({'error': f'Error checking run status: {str(e)}'})}\n\n"
            break
    else:
        yield f"data: {json.dumps({'error': 'Maximum retries reached'})}\n\n"

def format_response(content):
    try:
        response_data = json.loads(content)
        if 'response' in response_data and isinstance(response_data['response'], str):
            try:
                inner_json = json.loads(response_data['response'].strip('`').strip())
                if isinstance(inner_json, dict):
                    response_data = inner_json
            except json.JSONDecodeError:
                pass

        if 'response' not in response_data:
            raise ValueError("Invalid response format")

        # Ensure includes_products field is a boolean, and reflect correct state
        response_data['includes_products'] = bool(response_data.get('products', []))

        return json.dumps(response_data)
    except json.JSONDecodeError:
        return json.dumps({
            "response": content,
            "products": [],
            "includes_products": False
        })


def handle_required_action(run, thread_id):
    if run.required_action and run.required_action.type == "submit_tool_outputs":
        tool_outputs = []
        pre_shared_key = config.get('pre_shared_key', '')  # Get the pre-shared key from config
        
        for tool_call in run.required_action.submit_tool_outputs.tool_calls:
            if tool_call.function.name == "get_product_info":
                try:
                    arguments = json.loads(tool_call.function.arguments)
                    logger.debug(f"Handling required action for product ID {arguments['id']}")
                    product_info = get_product_info(
                        arguments['id'],
                        pre_shared_key,  # Use the pre-shared key from config
                        arguments['product_info_webhook_url']
                    )
                    tool_outputs.append({
                        "tool_call_id": tool_call.id,
                        "output": json.dumps(product_info)
                    })
                except Exception as e:
                    logger.error(f"Error in get_product_info: {str(e)}")
                    tool_outputs.append({
                        "tool_call_id": tool_call.id,
                        "output": json.dumps({"error": str(e)})
                    })

            if tool_call.function.name == "get_user_info":
                try:
                    arguments = json.loads(tool_call.function.arguments)
                    wp_username = arguments.get('wp_username', 'N/A')
                    session_wp_username = session.get('client_session_info', {}).get('wp_username')
                    logger.debug(f"Extracted wp_username before get_user_info call: {wp_username}")

                    # Verify if transformation happens
                    if wp_username != session_wp_username:
                        logger.warning(f"wp_username mismatch. Function arg: {wp_username}, Session: {session_wp_username}")
                        wp_username = session_wp_username

                    # Calling the get_user_info method with extracted wp_username
                    user_info = get_user_info(
                        wp_username,
                        pre_shared_key,  # Use the pre-shared key from config
                        arguments['user_info_webhook_url']
                    )

                    tool_outputs.append({
                        "tool_call_id": tool_call.id,
                        "output": json.dumps(user_info)
                    })
                except Exception as e:
                    logger.error(f"Error in get_user_info: {str(e)}")
                    tool_outputs.append({
                        "tool_call_id": tool_call.id,
                        "output": json.dumps({"error": str(e)})
                    })

        logger.debug(f"Prepared tool outputs: {tool_outputs}")

        # Only submit if tool outputs is not empty
        if tool_outputs:
            logger.debug(f"Submitting tool outputs: {tool_outputs}")
            try:
                client.beta.threads.runs.submit_tool_outputs(thread_id=thread_id, run_id=run.id, tool_outputs=tool_outputs)
            except OpenAIError as e:
                logger.error(f"Error submitting tool outputs: {str(e)}")
        else:
            logger.debug("No tool outputs to submit.")
        return True

    return False