import json
import time
import urllib.parse
import re
import requests
from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.db.models import F
from .models import ScannedHistory, SiteStats

# Persistent Session for TCP Keep-Alive connection reuse
session = requests.Session()

def landing_view(request):
    """Renders the SaaS landing page at root URL."""
    # Ensure a session exists so we can track first-visit state.
    if not request.session.session_key:
        request.session.create()

    stats = SiteStats.get_stats()

    # Only count the visitor once per unique browser session.
    if not request.session.get('visited_landing', False):
        request.session['visited_landing'] = True
        SiteStats.objects.filter(id=1).update(total_visitors=F('total_visitors') + 1)
        stats.refresh_from_db()

    display_visitors = stats.total_visitors
    display_requests = stats.total_requests_made

    return render(request, 'landing.html', {
        'display_visitors': display_visitors,
        'display_requests': display_requests
    })


def about_view(request):
    """Renders the About the Developer page."""
    return render(request, 'about.html')


def dashboard_view(request):
    """
    Renders the main bento dashboard page, loading all previously scanned Swagger urls.
    Filtered by the user's current session key to isolate data.
    """
    session_key = request.session.session_key
    if not session_key:
        history = ScannedHistory.objects.none()
    else:
        history = ScannedHistory.objects.filter(session_key=session_key).order_by('-scanned_at')
        
    return render(request, 'dashboard.html', {'history': history})


def qa_suite_view(request):
    """
    Renders the dedicated QA Automation Suite page, loading all previously scanned Swagger urls.
    Filtered by the user's current session key to isolate data.
    """
    session_key = request.session.session_key
    if not session_key:
        history = ScannedHistory.objects.none()
    else:
        history = ScannedHistory.objects.filter(session_key=session_key).order_by('-scanned_at')
        
    return render(request, 'qa_suite.html', {'history': history})


def resolve_refs(node, root_schema, resolved=None, memo=None):
    """
    Recursively resolves JSON references ($ref) in the Swagger/OpenAPI schema with memoization.
    Handles circular references gracefully and eliminates duplicate schema traversals.
    """
    if resolved is None:
        resolved = set()
    if memo is None:
        memo = {}
    
    if isinstance(node, dict):
        if '$ref' in node:
            ref_path = node['$ref']
            
            # Return cached resolution if available
            if ref_path in memo:
                return memo[ref_path]
                
            if ref_path in resolved:
                return {"type": "object", "description": f"Circular ref to {ref_path}"}
            
            parts = ref_path.lstrip('#/').split('/')
            target = root_schema
            try:
                for part in parts:
                    target = target[part]
                new_resolved = resolved | {ref_path}
                resolved_val = resolve_refs(target, root_schema, new_resolved, memo)
                # Cache resolution results
                memo[ref_path] = resolved_val
                return resolved_val
            except (KeyError, TypeError):
                return {"type": "object", "description": f"Unresolved ref {ref_path}"}
        else:
            return {k: resolve_refs(v, root_schema, resolved, memo) for k, v in node.items()}
    elif isinstance(node, list):
        return [resolve_refs(item, root_schema, resolved, memo) for item in node]
    return node

def resolve_schema_from_html(resp, base_url):
    """
    Attempts to detect if the HTML response is a login page or a Swagger/Redoc UI page,
    and extracts the actual raw OpenAPI spec JSON/YAML URL.
    """
    html_content = resp.text
    final_url = resp.url

    # 1. Check if the final redirected URL points to a login/admin page
    if any(term in final_url.lower() for term in ['/login', '/signin', '/admin/login']):
        raise ValueError("The request redirected to a login or admin page. This endpoint requires authentication.")

    # 2. Check if the HTML title suggests a login/authentication page
    title_match = re.search(r'<title>(.*?)</title>', html_content, re.IGNORECASE)
    if title_match:
        title_text = title_match.group(1).lower()
        if any(term in title_text for term in ['log in', 'login', 'sign in', 'signin', 'authorize', 'authentication']):
            raise ValueError("The URL returned a login page. This endpoint requires authentication.")

    # 3. Check for typical password inputs
    if 'name="password"' in html_content or 'type="password"' in html_content:
        raise ValueError("The URL returned a login page with password prompts. This endpoint requires authentication.")

    # 4. Search for common Swagger UI or Redoc configuration patterns (e.g. url: "...")
    patterns = [
        r'url\s*:\s*[\'"]([^\'\"]+)[\'"]',
        r'spec-url\s*=\s*[\'"]([^\'\"]+)[\'"]',
        r'data-url\s*=\s*[\'"]([^\'\"]+)[\'"]',
        r'SwaggerUIBundle\(\s*\{\s*url\s*:\s*[\'"]([^\'\"]+)[\'"]',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, html_content, re.IGNORECASE)
        if match:
            extracted_url = match.group(1)
            # Exclude asset paths
            if not extracted_url.endswith(('.js', '.css', '.png', '.jpg', '.gif', '.ico')):
                return urllib.parse.urljoin(base_url, extracted_url)

    raise ValueError(
        "The URL returned an HTML page instead of a raw OpenAPI JSON/YAML schema. "
        "If this is a Swagger UI page, we could not auto-detect the schema URL."
    )


@csrf_exempt
def parse_swagger(request):
    """
    POST endpoint: fetches a Swagger URL, parses/normalizes it,
    records it in ScannedHistory, and returns the schema tree.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST method required'}, status=405)
    
    swagger_url = None
    if request.content_type == 'application/json':
        try:
            data = json.loads(request.body)
            swagger_url = data.get('swagger_url') or data.get('url')
        except json.JSONDecodeError:
            pass
    else:
        swagger_url = request.POST.get('swagger_url') or request.POST.get('url')
        
    if not swagger_url:
        return JsonResponse({'error': 'Missing swagger_url parameter'}, status=400)
        
    # Standardize URL string
    swagger_url = swagger_url.strip()
    
    try:
        resp = session.get(swagger_url, timeout=15)
        resp.raise_for_status()
        
        content_type = resp.headers.get('content-type', '').lower()
        # If the response appears to be HTML, try to resolve the raw schema URL from it
        if 'text/html' in content_type or resp.text.strip().startswith('<!DOCTYPE') or resp.text.strip().startswith('<html'):
            try:
                real_schema_url = resolve_schema_from_html(resp, swagger_url)
                resp = session.get(real_schema_url, timeout=15)
                resp.raise_for_status()
            except ValueError as val_err:
                return JsonResponse({'error': f'Failed to fetch or parse Swagger URL: {str(val_err)}'}, status=400)
            except Exception as fetch_err:
                return JsonResponse({'error': f'Detected Swagger page but failed to fetch raw spec from {real_schema_url}: {str(fetch_err)}'}, status=400)

        try:
            schema = resp.json()
        except Exception as json_err:
            try:
                import yaml
                schema = yaml.safe_load(resp.text)
                if not isinstance(schema, dict):
                    raise ValueError("Parsed content is not a dictionary")
            except Exception as yaml_err:
                raise ValueError(f"Invalid JSON/YAML schema. JSON error: {str(json_err)}. YAML error: {str(yaml_err)}")
    except Exception as e:
        return JsonResponse({'error': f'Failed to fetch or parse Swagger URL: {str(e)}'}, status=400)
        
    # Extract API Title from Swagger/OpenAPI info block
    title = schema.get('info', {}).get('title', 'API Spec').strip()
    if not title:
        title = "Unnamed API"
        
    # Auto-create session if it doesn't exist
    if not request.session.session_key:
        request.session.create()
    session_key = request.session.session_key
        
    # Save/Update history in SQLite isolated by session
    ScannedHistory.objects.update_or_create(
        url=swagger_url,
        session_key=session_key,
        defaults={'title': title}
    )
    
    # Resolve all relative reference links
    try:
        resolved_schema = resolve_refs(schema, schema)
    except Exception as e:
        resolved_schema = schema
        
    # Auto-detect base server URL
    parsed_swagger_url = urllib.parse.urlparse(swagger_url)
    base_url = None
    
    # Check OpenAPI 3.x server configuration
    if 'servers' in resolved_schema and resolved_schema['servers']:
        for server in resolved_schema['servers']:
            url_str = server.get('url', '')
            if url_str:
                if url_str.startswith('http://') or url_str.startswith('https://'):
                    base_url = url_str
                else:
                    base_url = urllib.parse.urljoin(swagger_url, url_str)
                break
    
    # Check Swagger 2.0 host and basePath configuration
    elif 'host' in resolved_schema:
        schemes = resolved_schema.get('schemes', ['https'])
        scheme = schemes[0] if isinstance(schemes, list) and schemes else 'https'
        host = resolved_schema['host']
        base_path = resolved_schema.get('basePath', '')
        base_url = f"{scheme}://{host}{base_path}"
        
    # If no server URL was parsed, resolve to hostname of Swagger config url
    if not base_url:
        base_url = f"{parsed_swagger_url.scheme}://{parsed_swagger_url.netloc}"
        if 'basePath' in resolved_schema:
            base_url = urllib.parse.urljoin(base_url, resolved_schema['basePath'])
            
    endpoints_list = []
    paths = resolved_schema.get('paths', {})
    
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        
        # Paths can have global parameters
        path_parameters = path_item.get('parameters', [])
        
        for method in ['get', 'post', 'put', 'delete', 'patch', 'options', 'head']:
            if method not in path_item:
                continue
            
            op = path_item[method]
            if not isinstance(op, dict):
                continue
                
            op_parameters = op.get('parameters', [])
            
            # Build unified parameters mapping, overriding path levels
            params_dict = {}
            for p in path_parameters + op_parameters:
                if isinstance(p, dict):
                    params_dict[(p.get('name'), p.get('in'))] = p
            
            parameters_list = list(params_dict.values())
            
            # Resolve request body schemas (OpenAPI 3.x vs Swagger 2.0 body parameters)
            request_body_info = None
            if 'requestBody' in op:
                rb = op['requestBody']
                if isinstance(rb, dict):
                    required = rb.get('required', False)
                    content = rb.get('content', {})
                    media_type = 'application/json'
                    schema_details = {}
                    
                    # Inspect media types
                    if content:
                        if 'application/json' in content:
                            media_type = 'application/json'
                            schema_details = content['application/json'].get('schema', {})
                        elif 'application/x-www-form-urlencoded' in content:
                            media_type = 'application/x-www-form-urlencoded'
                            schema_details = content['application/x-www-form-urlencoded'].get('schema', {})
                        elif 'multipart/form-data' in content:
                            media_type = 'multipart/form-data'
                            schema_details = content['multipart/form-data'].get('schema', {})
                        else:
                            media_type = list(content.keys())[0]
                            schema_details = content[media_type].get('schema', {})
                            
                    request_body_info = {
                        'required': required,
                        'mediaType': media_type,
                        'schema': schema_details
                    }
            else:
                # Check for Swagger 2.0 format body parameters
                body_param = next((p for p in parameters_list if p.get('in') == 'body'), None)
                if body_param:
                    parameters_list = [p for p in parameters_list if p.get('in') != 'body']
                    request_body_info = {
                        'required': body_param.get('required', False),
                        'mediaType': 'application/json',
                        'schema': body_param.get('schema', {})
                    }

            # Parse success response schema (200/201) for UI generation
            response_schema = None
            response_description = ''
            responses = op.get('responses', {})
            for code in ['200', '201', '2XX', 'default']:
                resp_obj = responses.get(code)
                if not isinstance(resp_obj, dict):
                    continue
                response_description = resp_obj.get('description', '')
                # OpenAPI 3.x: content map
                content = resp_obj.get('content', {})
                if content:
                    for _mt, mt_val in content.items():
                        s = mt_val.get('schema', {})
                        if s:
                            response_schema = s
                            break
                # Swagger 2.0: inline schema
                elif 'schema' in resp_obj:
                    response_schema = resp_obj['schema']
                if response_schema:
                    break

            # Simplify and structure parameters for frontend generator
            formatted_params = []
            for p in parameters_list:
                formatted_params.append({
                    'name': p.get('name'),
                    'in': p.get('in'),
                    'required': p.get('required', False),
                    'type': p.get('type') or p.get('schema', {}).get('type', 'string'),
                    'description': p.get('description', ''),
                    'default': p.get('default') or p.get('schema', {}).get('default'),
                    'schema': p.get('schema', {})
                })

            # Detect authentication requirements
            has_security = False
            security = op.get('security') or resolved_schema.get('security')
            if security and isinstance(security, list) and len(security) > 0:
                has_security = True
            elif 'security' in op and (op['security'] == [] or op['security'] is None):
                has_security = False

            requires_auth = has_security or any(
                p.get('name', '').lower() in ['authorization', 'api_key', 'token', 'apikey', 'access_token']
                for p in formatted_params
            )

            endpoints_list.append({
                'path': path,
                'method': method.upper(),
                'summary': op.get('summary') or op.get('operationId') or f"{method.upper()} {path}",
                'description': op.get('description', ''),
                'parameters': formatted_params,
                'requestBody': request_body_info,
                'responseSchema': response_schema,
                'responseDescription': response_description,
                'tags': op.get('tags', ['default']),
                'requires_auth': requires_auth
            })
            
    raw_schema_summary = {
        'securityDefinitions': resolved_schema.get('securityDefinitions', {}),
        'security': resolved_schema.get('security', []),
        'components': {
            'securitySchemes': resolved_schema.get('components', {}).get('securitySchemes', {})
        }
    }
    
    return JsonResponse({
        'title': title,
        'description': resolved_schema.get('info', {}).get('description', ''),
        'version': resolved_schema.get('info', {}).get('version', ''),
        'baseUrl': base_url,
        'endpoints': endpoints_list,
        'raw_schema': raw_schema_summary
    })

@csrf_exempt
def proxy_request(request):
    """
    POST endpoint: Acts as a backend proxy to execute the client request
    from the dashboard, bypassing CORS, and returning status, latency, headers, and body.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST method required'}, status=405)
    
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON body'}, status=400)
    
    target_url = data.get('url')
    method = data.get('method', 'GET').upper()
    headers = data.get('headers', {})
    payload = data.get('body')
    
    if not target_url:
        return JsonResponse({'error': 'Missing url parameter'}, status=400)
        
    start_time = time.perf_counter()
    try:
        # Standardize requests arguments
        req_kwargs = {
            'headers': headers,
            'timeout': 20,
            'verify': False, # Avoid SSL cert verification issues during local/mock API simulation
        }
        
        # Load body payload depending on header type
        if payload is not None:
            if isinstance(payload, (dict, list)):
                req_kwargs['json'] = payload
            elif isinstance(payload, str):
                content_type = headers.get('Content-Type', '').lower()
                if 'application/json' in content_type:
                    try:
                        req_kwargs['json'] = json.loads(payload)
                    except ValueError:
                        req_kwargs['data'] = payload
                else:
                    req_kwargs['data'] = payload
            else:
                req_kwargs['data'] = payload
                
        # Fire proxy request using keep-alive session
        response = session.request(method, target_url, **req_kwargs)
        elapsed_time_ms = int((time.perf_counter() - start_time) * 1000)
        
        stats = SiteStats.get_stats()
        stats.total_requests_made = F('total_requests_made') + 1
        stats.save()
        
        # Try to parse response body as JSON
        is_json = False
        try:
            response_json = response.json()
            is_json = True
        except ValueError:
            response_json = None
            
        return JsonResponse({
            'status_code': response.status_code,
            'status_text': response.reason,
            'elapsed_time_ms': elapsed_time_ms,
            'headers': dict(response.headers),
            'body': response_json if is_json else response.text,
            'is_json': is_json
        })
    except requests.exceptions.RequestException as e:
        elapsed_time_ms = int((time.perf_counter() - start_time) * 1000)
        return JsonResponse({
            'error': str(e),
            'elapsed_time_ms': elapsed_time_ms
        }, status=502)

@csrf_exempt
def generate_ai_ui(request):
    """
    POST endpoint: Calls NVIDIA's API to generate a beautiful HTML layout
    representing the endpoint response based on its schema and the user's prompt.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST method required'}, status=405)
    
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON body'}, status=400)
    
    path = data.get('path', '')
    method = data.get('method', 'GET')
    schema = data.get('schema', {})
    prompt = data.get('prompt', '')
    model = data.get('model', 'moonshotai/kimi-k2.6').strip() or 'moonshotai/kimi-k2.6'
    api_key = data.get('api_key', '').strip()
    
    # Fallback to default API key if not provided
    if not api_key:
        api_key = "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"
        
    invoke_url = "https://integrate.api.nvidia.com/v1/chat/completions"
    
    # Structure system and user instructions
    system_instruction = (
        "You are an expert frontend developer. Your task is to generate a single-file, highly-polished, responsive HTML page "
        "that visualizes the JSON response data of a web API endpoint.\n\n"
        "Instructions:\n"
        "1. Styling: Use Tailwind CSS via CDN: <script src=\"https://cdn.tailwindcss.com\"></script>. Make the UI look premium, modern, "
        "and high-fidelity (use sleek dark or clean light palettes, harmonious accents, glassmorphism, nice shadows, rounded corners, "
        "good typography, and SVG icons).\n"
        "2. Interactivity & Updates: The page MUST contain a global JavaScript function `window.updateData(data)`. This function takes a "
        "JSON object representing the endpoint's response data and dynamically updates the page's HTML elements (text contents, image src attributes, "
        "badge colors, lists/rows, or other styles) to render the real API data in-place.\n"
        "3. Default Preview: Include a default mock data object (matching the schema structure) and invoke `updateData(mockData)` on initial load "
        "so the page immediately displays a complete and beautiful preview.\n"
        "4. Layout Guidelines: Make the layout responsive. Adjust based on content type (e.g., list/table for arrays, structured card for objects).\n"
        "5. Output Format: Output ONLY valid, self-contained HTML (enclosed in ```html and ``` blocks). Do not explain the code or add any other text "
        "outside the code block."
    )
    
    user_prompt = (
        f"API Endpoint: {method.upper()} {path}\n"
        f"Response Schema:\n{json.dumps(schema, indent=2)}\n\n"
        f"User Design Prompt / Custom Instructions:\n{prompt}\n\n"
        f"Generate the complete HTML page now."
    )
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": user_prompt}
        ],
        "max_tokens": 16384,
        "temperature": 1.00,
        "top_p": 1.00,
        "stream": False,
        "chat_template_kwargs": {"thinking": True}
    }
    
    try:
        response = requests.post(invoke_url, headers=headers, json=payload, timeout=120)
        if response.status_code != 200:
            return JsonResponse({
                'error': f"NVIDIA API Error (Status {response.status_code}): {response.text}"
            }, status=response.status_code)
            
        res_data = response.json()
        choices = res_data.get('choices', [])
        if not choices:
            return JsonResponse({'error': 'No response choices returned from AI model'}, status=502)
            
        ai_content = choices[0].get('message', {}).get('content', '')
        
        # Extract HTML block from markdown
        html_content = ""
        html_match = re.search(r"```html\s*(.*?)\s*```", ai_content, re.DOTALL | re.IGNORECASE)
        if html_match:
            html_content = html_match.group(1).strip()
        else:
            # Fallback if AI didn't wrap it properly
            if "<!DOCTYPE html>" in ai_content or "<html" in ai_content:
                html_content = ai_content.strip()
            else:
                return JsonResponse({'error': 'AI failed to generate a valid HTML block', 'raw_response': ai_content}, status=502)
                
        return JsonResponse({'html': html_content})
        
    except requests.exceptions.RequestException as e:
        return JsonResponse({'error': f"Request to NVIDIA API failed: {str(e)}"}, status=502)


@csrf_exempt
def analyze_auth(request):
    """
    POST endpoint: receives the parsed OpenAPI spec (endpoints + raw schema) and asks Kimi AI
    to detect the auth strategy, find the token endpoint, and return the credential fields
    needed so the frontend can build a targeted credentials form.
    Returns a structured JSON:
      {
        "auth_detected": true/false,
        "auth_type": "bearer"|"basic"|"apikey"|"none",
        "token_endpoint": "/auth/login",       # only for bearer
        "token_method": "POST",
        "token_field_username": "email",        # detected field names
        "token_field_password": "password",
        "token_json_path": "data.access_token", # path in response to extract token
        "apikey_header_name": "X-API-Key",      # only for apikey
        "description": "human-readable explanation"
      }
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST method required'}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON body'}, status=400)

    endpoints = data.get('endpoints', [])
    raw_schema = data.get('raw_schema', {})

    if not endpoints:
        return JsonResponse({'auth_detected': False, 'auth_type': 'none', 'description': 'No endpoints provided.'})

    invoke_url = "https://integrate.api.nvidia.com/v1/chat/completions"
    api_key = "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"
    model = "moonshotai/kimi-k2.6"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

    system_instruction = (
        "You are an expert API Security Analyst. Analyze the provided OpenAPI schema and endpoint list "
        "and detect the authentication strategy used.\n\n"
        "Your task:\n"
        "1. Look at 'securitySchemes', 'security' fields, and any endpoint that looks like a login/token/auth endpoint.\n"
        "2. Determine the auth type: 'bearer', 'basic', 'apikey', or 'none'.\n"
        "3. If bearer: find the token acquisition endpoint (e.g. POST /auth/login, POST /users/login, POST /token) "
        "and its exact request body field names (e.g. 'email', 'username', 'phone', 'password', 'secret', etc.).\n"
        "4. If the token is nested in the response JSON, detect the JSON path (e.g. 'token', 'data.access_token', 'result.token').\n"
        "5. If apikey: detect the header name (e.g. 'X-API-Key', 'Authorization').\n"
        "6. If basic: confirm username/password field names if specified.\n"
        "7. Output ONLY valid JSON inside a ```json ... ``` block. No explanation outside it.\n\n"
        "Required JSON schema:\n"
        "{\n"
        "  \"auth_detected\": true,\n"
        "  \"auth_type\": \"bearer\",\n"
        "  \"token_endpoint\": \"/auth/login\",\n"
        "  \"token_method\": \"POST\",\n"
        "  \"token_request_body\": {\"email\": \"string\", \"password\": \"string\"},\n"
        "  \"token_json_path\": \"token\",\n"
        "  \"apikey_header_name\": null,\n"
        "  \"description\": \"Bearer token auth via POST /auth/login using email and password. Token found at response.token.\"\n"
        "}\n"
        "If no auth is detected: set auth_detected=false and auth_type='none'.\n"
        "CRITICAL: token_request_body must be a dict of field_name: field_type pairs from the actual schema."
    )

    # Send only top 30 endpoints and securitySchemes to keep context small
    schema_summary = {
        "securitySchemes": raw_schema.get('securityDefinitions') or raw_schema.get('components', {}).get('securitySchemes', {}),
        "security": raw_schema.get('security', []),
        "endpoints_sample": endpoints[:30]
    }

    user_prompt = (
        f"Analyze this OpenAPI schema and detect the authentication strategy:\n"
        f"{json.dumps(schema_summary, indent=2)}\n\n"
        f"Return the auth detection JSON now."
    )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": user_prompt}
        ],
        "max_tokens": 2048,
        "temperature": 0.3,
        "top_p": 1.00,
        "stream": False,
        "chat_template_kwargs": {"thinking": True}
    }

    print("\n" + "="*80)
    print("AI AUTH ANALYSIS REQUEST")
    print(f"URL: {invoke_url}")
    print(f"Body:\n{json.dumps(payload, indent=2)}")
    print("="*80 + "\n")

    try:
        response = requests.post(invoke_url, headers=headers, json=payload, timeout=45)
        print(f"AI AUTH RESPONSE STATUS: {response.status_code}")

        if response.status_code == 200:
            resp_json = response.json()
            ai_content = resp_json.get('choices', [{}])[0].get('message', {}).get('content', '')
            print(f"AI AUTH RESPONSE CONTENT:\n{ai_content}\n")

            # Extract JSON block
            json_match = re.search(r"```json\s*(.*?)\s*```", ai_content, re.DOTALL | re.IGNORECASE)
            if json_match:
                json_str = json_match.group(1).strip()
            else:
                braces = re.search(r"({.*})", ai_content, re.DOTALL)
                json_str = braces.group(1).strip() if braces else '{}'

            auth_data = json.loads(json_str)
            return JsonResponse(auth_data)
        else:
            print(f"AI API error: {response.status_code} {response.text}")
            return JsonResponse({'auth_detected': False, 'auth_type': 'none', 'description': f'AI API error: {response.status_code}'})

    except Exception as e:
        print(f"Auth analysis failed: {str(e)}")
        return JsonResponse({'auth_detected': False, 'auth_type': 'none', 'description': f'Analysis failed: {str(e)}'})


@csrf_exempt
def generate_test_plan(request):
    """
    POST endpoint: analyzes the provided OpenAPI endpoints and generates a structured test plan.
    Uses NVIDIA Integrate API (meta/llama-3.3-70b-instruct) to generate the plan dynamically in chunks
    to avoid context size limits and read timeouts (60s).
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST method required'}, status=405)
    
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON body'}, status=400)
        
    endpoints = data.get('endpoints', [])
    test_types = data.get('test_types', [])
    
    if not endpoints:
        return JsonResponse({'error': 'No endpoints provided to generate plan'}, status=400)

    # Chunk size configuration
    chunk_size = 5
    endpoint_chunks = [endpoints[i:i + chunk_size] for i in range(0, len(endpoints), chunk_size)]
    
    def sse_test_plan_generator():
        final_steps = []
        final_required_inputs = []
        global_step_id = 1
        ai_calls = []
        
        invoke_url = "https://integrate.api.nvidia.com/v1/chat/completions"
        api_key = "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"
        model = "moonshotai/kimi-k2.6"
        
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "text/event-stream",
            "Content-Type": "application/json"
        }
        
        system_instruction = (
            "You are an expert QA Automation Engineer. Your task is to analyze OpenAPI endpoints and generate "
            "a comprehensive, structured test plan based on the user's selected test types.\n\n"
            "Instructions:\n"
            "1. Analyze all endpoints, their paths, methods, parameters, request bodies, and auth requirements.\n"
            "2. For each endpoint, generate test steps mapping directly to the selected test types (e.g., 'Normal Test', 'Authentication Test', 'Responses Expectations Test', 'Security & Injection Test', 'Performance/Latency Test').\n"
            "3. Ensure the test steps are logical and comprehensive.\n"
            "4. Output ONLY valid, parseable JSON inside a ```json ... ``` markdown block. Do NOT include any conversation or explanation before/after the JSON block.\n\n"
            "The JSON response must match the following schema:\n"
            "{\n"
            "  \"test_plan_summary\": \"A short high-level summary of the entire test plan\",\n"
            "  \"required_inputs\": [\n"
            "     {\n"
            "        \"type\": \"auth_token_or_credentials\",\n"
            "        \"description\": \"Describe what auth credentials (token, apikey, basic auth) are required to run tests on protected endpoints.\"\n"
            "     }\n"
            "  ],\n"
            "  \"steps\": [\n"
            "     {\n"
            "        \"id\": 1,\n"
            "        \"endpoint\": \"/api/v1/users\",\n"
            "        \"method\": \"POST\",\n"
            "        \"test_type\": \"Normal Test\",\n"
            "        \"description\": \"A highly readable, specific step description describing the exact dummy inputs to use and the expected success response status (e.g., 201 Created).\",\n"
            "        \"estimated_requests\": 1\n"
            "     }\n"
            "  ]\n"
            "}"
        )

        for chunk_idx, chunk in enumerate(endpoint_chunks):
            yield f"data: {json.dumps({'type': 'status', 'message': f'Processing endpoint chunk {chunk_idx + 1}/{len(endpoint_chunks)} (Size: {len(chunk)})'})}\n\n"
            
            # 1. Generate local fallback plan for this chunk just in case
            local_chunk_steps = []
            local_chunk_required = []
            local_step_id = global_step_id
            
            has_auth_endpoints = any(ep.get('requires_auth', False) for ep in chunk)
            if has_auth_endpoints:
                local_chunk_required.append({
                    "type": "auth_token_or_credentials",
                    "description": "Auth credentials are required to run tests on protected endpoints in this section."
                })
                
            for ep in chunk:
                path = ep.get('path')
                method = ep.get('method')
                requires_auth = ep.get('requires_auth', False)
                params = ep.get('parameters', [])
                req_body = ep.get('requestBody')
                
                if 'Authentication Test' in test_types and requires_auth:
                    local_chunk_steps.append({
                        "id": local_step_id,
                        "endpoint": path,
                        "method": method,
                        "test_type": "Authentication Test",
                        "description": f"Call {method} {path} without auth headers. Expected: 401 Unauthorized or 403 Forbidden.",
                        "estimated_requests": 1
                    })
                    local_step_id += 1
                    
                if 'Normal Test' in test_types:
                    auth_desc = " with auth credentials" if requires_auth else ""
                    local_chunk_steps.append({
                        "id": local_step_id,
                        "endpoint": path,
                        "method": method,
                        "test_type": "Normal Test",
                        "description": f"Send a standard {method} request{auth_desc} using valid dummy data. Expected: 200/201 Success.",
                        "estimated_requests": 1
                    })
                    local_step_id += 1
                    
                if 'Responses Expectations Test' in test_types:
                    fuzz_targets = []
                    for p in params:
                        if p.get('required'):
                            fuzz_targets.append(f"omit required parameter '{p.get('name')}'")
                        if p.get('type') == 'integer':
                            fuzz_targets.append(f"send non-integer value for '{p.get('name')}'")
                        elif p.get('type') == 'string' and 'email' in p.get('name', '').lower():
                            fuzz_targets.append(f"send invalid email format for '{p.get('name')}'")
                    
                    if req_body and req_body.get('schema'):
                        schema = req_body.get('schema', {})
                        req_fields = schema.get('required', [])
                        if req_fields:
                            fuzz_targets.append(f"omit required body fields: {', '.join(req_fields)}")
                        
                    if not fuzz_targets:
                        fuzz_targets.append("send empty or invalid body payload")
                        
                    for target in fuzz_targets[:3]:
                        local_chunk_steps.append({
                            "id": local_step_id,
                            "endpoint": path,
                            "method": method,
                            "test_type": "Responses Expectations Test",
                            "description": f"Fuzz {method} {path}: {target}. Expected: 400 Bad Request or validation error (no 500s).",
                            "estimated_requests": 1
                        })
                        local_step_id += 1
                        
                if 'Security & Injection Test' in test_types:
                    string_params = [p.get('name') for p in params if p.get('type') == 'string']
                    has_body = req_body is not None
                    if string_params or has_body:
                        local_chunk_steps.append({
                            "id": local_step_id,
                            "endpoint": path,
                            "method": method,
                            "test_type": "Security & Injection Test",
                            "description": f"Inject common SQLi/XSS payloads into {method} {path} fields to check input handling. Expected: 400 Bad Request or safe sanitization.",
                            "estimated_requests": 3
                        })
                        local_step_id += 1
                        
                if 'Performance/Latency Test' in test_types:
                    local_chunk_steps.append({
                        "id": local_step_id,
                        "endpoint": path,
                        "method": method,
                        "test_type": "Performance/Latency Test",
                        "description": f"Measure latency for {method} {path} under rapid requests (10 calls). Expected: average latency < 500ms.",
                        "estimated_requests": 10
                    })
                    local_step_id += 1

            # 2. Call AI API for this chunk
            user_prompt = (
                f"Generate a test plan for this section of OpenAPI endpoints (Section {chunk_idx + 1}/{len(endpoint_chunks)}):\n"
                f"Endpoints:\n{json.dumps(chunk, indent=2)}\n\n"
                f"Selected Test Types to include:\n{json.dumps(test_types, indent=2)}\n\n"
                f"Generate the JSON test plan now."
            )
            
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": user_prompt}
                ],
                "max_tokens": 16384,
                "temperature": 1.00,
                "top_p": 1.00,
                "stream": True,
                "chat_template_kwargs": {"thinking": True}
            }
            
            # Log to Django server console
            print("\n" + "="*80)
            print(f"AI AGENT REQUEST (PLAN CHUNK {chunk_idx + 1}/{len(endpoint_chunks)})")
            print(f"URL: {invoke_url}")
            print(f"Body:\n{json.dumps(payload, indent=2)}")
            print("="*80 + "\n")
            
            chunk_success = False
            ai_response_val = None
            full_chunk_text = ""
            
            try:
                # Set timeout to 30s per chunk to fail fast and fallback
                response = requests.post(invoke_url, headers=headers, json=payload, stream=True, timeout=30)
                
                print("\n" + "="*80)
                print(f"AI AGENT RESPONSE (PLAN CHUNK {chunk_idx + 1}/{len(endpoint_chunks)}) - STATUS {response.status_code}")
                
                if response.status_code == 200:
                    for line in response.iter_lines():
                        if line:
                            line_str = line.decode('utf-8')
                            if line_str.startswith("data: "):
                                data_content = line_str[6:].strip()
                                if data_content == "[DONE]":
                                    break
                                try:
                                    chunk_json = json.loads(data_content)
                                    token = chunk_json.get('choices', [{}])[0].get('delta', {}).get('content', '')
                                    if token:
                                        full_chunk_text += token
                                        # Forward token to client terminal in real-time
                                        yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"
                                except Exception:
                                    pass
                    
                    print(f"Accumulated Text:\n{full_chunk_text}")
                    print("="*80 + "\n")
                    
                    # Try to parse the accumulated text
                    ai_content = full_chunk_text
                    json_match = re.search(r"```json\s*(.*?)\s*```", ai_content, re.DOTALL | re.IGNORECASE)
                    if json_match:
                        json_str = json_match.group(1).strip()
                    else:
                        braces_match = re.search(r"({.*})", ai_content, re.DOTALL)
                        if braces_match:
                            json_str = braces_match.group(1).strip()
                        else:
                            json_str = ai_content.strip()
                    
                    plan_data = json.loads(json_str)
                    if isinstance(plan_data, dict) and "steps" in plan_data and isinstance(plan_data["steps"], list):
                        # Ensure fields exist
                        for s in plan_data["steps"]:
                            if "endpoint" not in s or "method" not in s or "test_type" not in s or "description" not in s:
                                raise ValueError("Missing step keys")
                        
                        # Add parsed steps with sequentially updated step IDs
                        chunk_steps_added = 0
                        for s in plan_data["steps"]:
                            s["id"] = global_step_id
                            final_steps.append(s)
                            global_step_id += 1
                            chunk_steps_added += 1
                        
                        # Accumulate required inputs
                        req_inputs = plan_data.get("required_inputs", [])
                        for ri in req_inputs:
                            if ri not in final_required_inputs:
                                final_required_inputs.append(ri)
                                
                        chunk_success = True
                        ai_response_val = plan_data
                        yield f"data: {json.dumps({'type': 'status', 'message': f'Successfully parsed {chunk_steps_added} steps from chunk {chunk_idx + 1}.'})}\n\n"
                else:
                    ai_response_val = {"error": f"NVIDIA API Error status: {response.status_code}"}
                            
            except Exception as e:
                print(f"AI Plan request for chunk {chunk_idx + 1} failed, using local generator: {str(e)}")
                ai_response_val = {"error": f"Request failed: {str(e)}. Fallback to rule-based generation used."}
                yield f"data: {json.dumps({'type': 'status', 'message': f'Chunk {chunk_idx + 1} failed: {str(e)}. Falling back to local rule-based generation.'})}\n\n"
                
            if not chunk_success:
                # Apply local fallback for this chunk
                print(f"Applying rule-based local steps fallback for chunk {chunk_idx + 1}")
                fallback_added = 0
                for s in local_chunk_steps:
                    s["id"] = global_step_id
                    final_steps.append(s)
                    global_step_id += 1
                    fallback_added += 1
                    
                for ri in local_chunk_required:
                    if ri not in final_required_inputs:
                        final_required_inputs.append(ri)
                yield f"data: {json.dumps({'type': 'status', 'message': f'Applied local fallback: generated {fallback_added} steps for chunk {chunk_idx + 1}.'})}\n\n"
                        
            ai_calls.append({
                "request_body": payload,
                "response_body": ai_response_val or {"message": "No response due to failure / fallback usage"}
            })
            yield f"data: {json.dumps({'type': 'chunk_done'})}\n\n"

        # Global summary calculation
        final_summary = f"Comprehensive test plan generated dynamically in {len(endpoint_chunks)} sections with {len(final_steps)} steps across {len(endpoints)} endpoints."
        
        final_plan = {
            "test_plan_summary": final_summary,
            "required_inputs": final_required_inputs,
            "steps": final_steps,
            "ai_request_url": invoke_url,
            "ai_calls": ai_calls
        }
        
        yield f"data: {json.dumps({'type': 'final_plan', 'plan': final_plan})}\n\n"

    from django.http import StreamingHttpResponse
    return StreamingHttpResponse(sse_test_plan_generator(), content_type="text/event-stream")


@csrf_exempt
def generate_test_report(request):
    """
    POST endpoint: compiles execution results into a beautiful Markdown report.
    Streams report compilation in real-time using NVIDIA completions API.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST method required'}, status=405)
        
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON body'}, status=400)
        
    steps_results = data.get('results', [])
    if not steps_results:
        return JsonResponse({'error': 'No test results provided'}, status=400)
        
    total_run = len(steps_results)
    passed = sum(1 for r in steps_results if r.get('status') == 'passed')
    failed = total_run - passed
    success_rate = round((passed / total_run) * 100, 1) if total_run > 0 else 0
    
    # 1. Deterministic Fallback Report
    by_type = {}
    for r in steps_results:
        tt = r.get('test_type', 'Other Test')
        if tt not in by_type:
            by_type[tt] = []
        by_type[tt].append(r)
        
    md = []
    md.append("# 📊 API Test Execution Report\n")
    md.append("## 1. Executive Summary")
    md.append(f"* **Total Tests Run:** {total_run}")
    md.append(f"* **Passed:** {passed} ✅")
    md.append(f"* **Failed:** {failed} ❌")
    md.append(f"* **Success Rate:** {success_rate}%\n")
    md.append("## 2. Detailed Breakdown by Test Type\n")
    
    for tt, results in by_type.items():
        md.append(f"### {tt}s")
        for r in results:
            status_symbol = "Passed ✅" if r.get('status') == 'passed' else "FAILED ❌"
            method = r.get('method', 'GET')
            endpoint = r.get('endpoint', '/')
            details = r.get('details', '')
            md.append(f"* **[{method}] {endpoint}**: {status_symbol} ({details})")
        md.append("")
        
    md.append("## 3. Key Findings & Recommendations")
    critical_bugs = []
    recommendations = []
    
    auth_fails = [r for r in steps_results if r.get('test_type') == 'Authentication Test' and r.get('status') == 'failed']
    server_errors = [r for r in steps_results if '500' in r.get('details', '') or 'Internal Server Error' in r.get('details', '')]
    security_fails = [r for r in steps_results if r.get('test_type') == 'Security & Injection Test' and r.get('status') == 'failed']
    perf_fails = [r for r in steps_results if r.get('test_type') == 'Performance/Latency Test' and r.get('status') == 'failed']
    
    if auth_fails:
        critical_bugs.append(f"Security Risk: {len(auth_fails)} protected endpoint(s) allowed access without authentication.")
        recommendations.append("Ensure authentication middleware is correctly applied to all private routes.")
    if server_errors:
        critical_bugs.append(f"System Stability: {len(server_errors)} endpoint(s) threw 500 Internal Server Errors under invalid payloads.")
        recommendations.append("Implement proper catch-all error handling and request validation schemas in your controllers.")
    if security_fails:
        critical_bugs.append(f"Injection Vulnerabilities: Detected potential SQL Injection or XSS vulnerabilities in string inputs.")
        recommendations.append("Use parameterized database queries/ORMs and sanitize string fields before querying.")
    if perf_fails:
        critical_bugs.append(f"Performance Issues: Average latency exceeded limits on {len(perf_fails)} endpoints.")
        recommendations.append("Optimize database queries and integrate a caching layer.")
        
    if not critical_bugs:
        critical_bugs.append("None. All endpoints handled valid and invalid inputs as expected according to the OpenAPI schema specifications.")
        recommendations.append("Continue monitoring API performance and maintain current validation constraints.")
        
    for bug in critical_bugs:
        md.append(f"* {bug}")
    md.append("\n**Recommendations:**")
    for rec in recommendations:
        md.append(f"* {rec}")
        
    fallback_report = "\n".join(md)

    def sse_report_generator():
        invoke_url = "https://integrate.api.nvidia.com/v1/chat/completions"
        api_key = "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"
        model = "moonshotai/kimi-k2.6"
        
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "text/event-stream",
            "Content-Type": "application/json"
        }
        
        system_instruction = (
            "You are an expert Lead QA Automation Director. Your task is to analyze the API test execution results "
            "and compile a detailed, beautiful, and highly professional Markdown Test Execution Report.\n\n"
            "Instructions:\n"
            "1. Write a premium-looking markdown report.\n"
            "2. Include an 'Executive Summary' section with a clean table of totals (run, passed, failed, success rate).\n"
            "3. Include a 'Detailed Breakdown by Test Type' summarizing how each endpoint performed.\n"
            "4. Include a 'Key Findings' section highlighting security risks (e.g., private endpoints that succeeded without auth), stability issues (e.g., 500 server errors), or injection vulnerabilities.\n"
            "5. Include concrete, actionable developer recommendations for resolving each failure category.\n"
            "6. Return ONLY raw Markdown text. Do NOT wrap it in HTML or other formats."
        )
        
        user_prompt = (
            f"Compile a Test Execution Report based on these results:\n"
            f"Total Tests Run: {total_run}\n"
            f"Passed: {passed}\n"
            f"Failed: {failed}\n"
            f"Success Rate: {success_rate}%\n\n"
            f"Detailed Results List:\n{json.dumps(steps_results, indent=2)}\n\n"
            f"Compile the markdown report now."
        )
        
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_prompt}
            ],
            "max_tokens": 16384,
            "temperature": 1.00,
            "top_p": 1.00,
            "stream": True,
            "chat_template_kwargs": {"thinking": True}
        }
        
        yield f"data: {json.dumps({'type': 'status', 'message': 'Compiling AI Lead Analyst Report...'})}\n\n"

        full_markdown = ""
        chunk_success = False
        
        try:
            response = requests.post(invoke_url, headers=headers, json=payload, stream=True, timeout=60)
            if response.status_code == 200:
                for line in response.iter_lines():
                    if line:
                        line_str = line.decode('utf-8')
                        if line_str.startswith("data: "):
                            data_content = line_str[6:].strip()
                            if data_content == "[DONE]":
                                break
                            try:
                                chunk_json = json.loads(data_content)
                                token = chunk_json.get('choices', [{}])[0].get('delta', {}).get('content', '')
                                if token:
                                    full_markdown += token
                                    yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"
                            except Exception:
                                pass
                if full_markdown.strip():
                    chunk_success = True
        except Exception as e:
            print(f"AI Report streaming failed: {str(e)}")

        if not chunk_success:
            yield f"data: {json.dumps({'type': 'status', 'message': 'AI report compilation failed. Falling back to static report.'})}\n\n"
            full_markdown = fallback_report
            yield f"data: {json.dumps({'type': 'token', 'content': fallback_report})}\n\n"

        summary_data = {
            "total": total_run,
            "passed": passed,
            "failed": failed,
            "success_rate": success_rate
        }
        
        yield f"data: {json.dumps({'type': 'final_report', 'report_markdown': full_markdown, 'summary': summary_data, 'ai_request_url': invoke_url, 'ai_request_body': payload, 'ai_response_body': {'message': 'Streamed successfully'}})}\n\n"

    from django.http import StreamingHttpResponse
    return StreamingHttpResponse(sse_report_generator(), content_type="text/event-stream")
