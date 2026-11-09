"""Keyword matching between a job description and a resume, the way ATS filters score them."""

import re

# canonical keyword -> regex of accepted spellings
VOCAB = {
    # languages
    "Python": r"python", "Java": r"\bjava\b(?!script)", "JavaScript": r"javascript|\bjs\b",
    "TypeScript": r"typescript|\bts\b", "C++": r"c\+\+|\bcpp\b", "C#": r"c#|\.net\b",
    "Go": r"\bgolang\b|\bgo,|, go\b|/go\b|\bgo/",
    "Rust": r"\brust\b", "Kotlin": r"kotlin", "Swift": r"\bswift\b", "Scala": r"\bscala\b",
    "Ruby": r"\bruby\b", "SQL": r"\bsql\b", "MATLAB": r"matlab",
    "HTML/CSS": r"\bhtml\b|\bcss\b", "Bash": r"\bbash\b|shell script",
    # frameworks / tools
    "React": r"\breact\b(?! native)", "React Native": r"react native", "Next.js": r"next\.?js",
    "Node.js": r"node\.?js|\bnode\b", "Angular": r"angular", "Vue": r"\bvue",
    "Django": r"django", "Flask": r"flask", "FastAPI": r"fastapi", "Spring": r"spring (boot|framework|mvc)",
    "GraphQL": r"graphql", "REST APIs": r"\brest(ful)?\b|\bapis?\b",
    "PostgreSQL": r"postgres", "MySQL": r"mysql", "MongoDB": r"mongo", "Redis": r"redis",
    "NoSQL": r"nosql", "Prisma": r"prisma", "Kafka": r"kafka", "Spark": r"apache spark|pyspark|\bspark\b(?= |,|/)(?!.{0,20}(joy|curiosity|interest))",
    "AWS": r"\baws\b|amazon web services", "GCP": r"\bgcp\b|google cloud", "Azure": r"azure",
    "Docker": r"docker|container", "Kubernetes": r"kubernetes|\bk8s\b|\beks\b|\bgke\b",
    "Terraform": r"terraform", "CI/CD": r"ci/cd|continuous integration|github actions|jenkins",
    "Git": r"\bgit\b|github|gitlab", "Linux": r"linux|unix", "Microservices": r"microservice",
    "Distributed systems": r"distributed system", "Cloud": r"\bcloud\b",
    "iOS": r"\bios\b", "Android": r"android", "Mobile": r"(?<!t-)\bmobile (app|dev|eng|platform)",
    "Frontend": r"front[- ]?end", "Backend": r"back[- ]?end", "Full-stack": r"full[- ]?stack",
    "Testing": r"unit test|testing|\btests?\b|pytest|jest", "Debugging": r"debug",
    "Data structures & algorithms": r"data structures|algorithms",
    "Object-oriented programming": r"object[- ]oriented|\boop\b",
    "System design": r"system design|scalab", "Security": r"security",
    # more languages, tools and platforms
    "PHP": r"\bphp\b", "Perl": r"\bperl\b", "Dart": r"\bdart\b", "Flutter": r"flutter", "Haskell": r"haskell",
    "Elixir": r"elixir", "Lua": r"\blua\b", "Verilog": r"verilog|systemverilog", "PowerShell": r"powershell",
    "Express": r"express\.?js", "Svelte": r"svelte", "Tailwind": r"tailwind", "Redux": r"redux",
    "jQuery": r"jquery", ".NET": r"asp\.net|\.net (core|framework)", "Ruby on Rails": r"\brails\b", "Laravel": r"laravel",
    "SwiftUI": r"swiftui", "Jetpack Compose": r"jetpack compose", "Xcode": r"xcode",
    "Unity": r"\bunity (3d|engine|game)|\bunity\b(?=,)", "Unreal Engine": r"unreal engine", "OpenGL": r"opengl|vulkan|directx",
    "CUDA": r"\bcuda\b", "ROS": r"\bros2?\b", "Embedded systems": r"embedded", "Firmware": r"firmware", "RTOS": r"\brtos\b",
    "gRPC": r"grpc|protobuf|protocol buffers", "WebSockets": r"websocket", "OAuth": r"oauth|openid",
    "OpenAPI / Swagger": r"openapi|swagger", "Postman": r"\bpostman\b",
    "GitHub Actions": r"github actions", "Jenkins": r"jenkins", "Ansible": r"ansible", "Helm": r"\bhelm\b",
    "Argo CD": r"argo ?cd|gitops", "Prometheus": r"prometheus", "Grafana": r"grafana", "Datadog": r"\bdatadog\b(?! is| inc)",
    "Splunk": r"splunk", "Observability / monitoring": r"observability|monitoring|alerting|on-call",
    "Networking": r"computer networking|networking fundamentals|tcp/ip|\bdns\b|load balanc",
    "Shell scripting": r"shell scripting|bash scripting|scripting languages?",
    "Serverless": r"serverless|aws lambda|cloud functions",
    "Infrastructure as code": r"infrastructure[- ]as[- ]code|\biac\b|cloudformation|pulumi|\bbicep\b",
    "Elasticsearch": r"elasticsearch|opensearch", "Message queues": r"rabbitmq|message queue|\bsqs\b|pub/sub",
    "DynamoDB": r"dynamodb", "Firebase": r"firebase", "Supabase": r"supabase", "SQLite": r"sqlite",
    "Snowflake": r"snowflake", "BigQuery": r"bigquery", "Databricks": r"\bdatabricks\b(?! is)", "Airflow": r"airflow",
    "dbt": r"\bdbt\b", "Hadoop": r"hadoop", "Data modeling": r"data model", "Data visualization": r"data visuali|dashboard",
    "Power BI": r"power ?bi\b", "Jupyter": r"jupyter", "Matplotlib": r"matplotlib|seaborn|plotly",
    "scikit-learn": r"scikit|sklearn", "Hugging Face": r"hugging ?face",
    "LLMs / prompt engineering": r"large language model|prompt engineering|\brag\b|langchain|\bllms?\b",
    "Computer vision": r"computer vision|opencv|image recognition", "NLP": r"\bnlp\b|natural language processing",
    "Selenium": r"selenium", "Cypress": r"\bcypress\b", "Playwright": r"\bplaywright\b", "JUnit": r"junit",
    "Integration testing": r"integration test|end-to-end test|e2e test", "Code review": r"code review",
    "Concurrency": r"concurren|multithread|multi-thread|parallel programming",
    "Operating systems": r"operating systems", "Compilers": r"compiler", "Databases": r"\bdatabases?\b|\bdbms\b",
    "Cybersecurity": r"cybersecurity|cyber security|penetration test|vulnerabilit|threat model",
    "Accessibility": r"accessibility|\bwcag\b|\ba11y\b", "Responsive design": r"responsive (web|design|ui)",
    "Salesforce": r"salesforce", "ServiceNow": r"servicenow",
    "Mixpanel / Amplitude": r"mixpanel|amplitude|google analytics|product analytics",
    "SQL analytics": r"sql (queries|analysis|analytics)|write sql",
    # data / ML
    "Machine learning": r"machine learning|\bml\b", "AI": r"\bai\b|artificial intelligence|\bllms?\b",
    "Deep learning": r"deep learning|neural net", "PyTorch": r"pytorch", "TensorFlow": r"tensorflow",
    "Pandas": r"pandas", "NumPy": r"numpy", "Data analysis": r"data analy|analytics",
    "Data pipelines": r"pipeline|\betl\b", "Statistics": r"statistic",
    "Tableau": r"tableau", "Excel": r"\bexcel\b|spreadsheet", "Looker": r"looker",
    # PM
    "Product management": r"product manag", "Roadmap": r"roadmap", "Product strategy": r"product strategy|product vision",
    "User research": r"user research|user interview|customer interview|usability",
    "A/B testing": r"a/b test|experiment", "Metrics / KPIs": r"\bkpis?\b|metrics|okrs?",
    "Requirements / PRDs": r"\bprds?\b|product requirement|user stor", "Agile": r"agile|scrum|sprint",
    "Jira": r"\bjira\b", "Figma": r"figma", "Wireframing / prototyping": r"wirefram|prototyp",
    "UX design": r"\bux\b|user experience|human-computer|\bhci\b", "Go-to-market": r"go-to-market|\bgtm\b|launch",
    "Stakeholder management": r"stakeholder", "Cross-functional": r"cross[- ]functional",
    "Market research": r"market research|competitive analysis", "Prioritization": r"prioriti",
    "Customer focus": r"customer",
    # soft skills recruiters screen for
    "Communication": r"communicat", "Leadership": r"leader", "Collaboration": r"collaborat|teamwork",
    "Problem solving": r"problem[- ]solv", "Ownership": r"ownership|take initiative|self-starter",
}

_COMPILED = {k: re.compile(v, re.I) for k, v in VOCAB.items()}

# Soft skills: worth echoing in bullets, but rarely what an ATS filter screens on.
SOFT = {"Communication", "Leadership", "Collaboration", "Problem solving", "Ownership", "Customer focus"}


def found(text):
    return {k for k, rx in _COMPILED.items() if rx.search(text or "")}


def match(jd, resume, company=None):
    """Keywords the JD asks for, split by whether the resume already has them.

    `company`: the posting's company, so its own name ("Databricks", "Datadog") isn't
    counted as a skill."""
    want = found(jd)
    if company:
        co = company.lower()
        want = {k for k in want if k.lower() not in co}
    have = found(resume)
    hit = sorted(want & have)
    miss = sorted(want - have)
    score = round(100 * len(hit) / len(want)) if want else 0
    return {"score": score, "matched": hit, "missing": miss}
