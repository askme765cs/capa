# Copyright (C) 2020 FireEye, Inc. All Rights Reserved.
# Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
# You may obtain a copy of the License at: [package root]/LICENSE.txt
# Unless required by applicable law or agreed to in writing, software distributed under the License
#  is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and limitations under the License.

import json

import capa.rules
import capa.engine
import capa.render.utils


def convert_statement_to_result_document(statement):
    """
    "statement": {
        "type": "or"
    },

    "statement": {
        "max": 9223372036854775808,
        "min": 2,
        "type": "range"
    },
    """
    statement_type = statement.name.lower()
    result = {"type": statement_type}
    if statement.description:
        result["description"] = statement.description

    if statement_type == "some" and statement.count == 0:
        result["type"] = "optional"
    elif statement_type == "some":
        result["count"] = statement.count
    elif statement_type == "range":
        result["min"] = statement.min
        result["max"] = statement.max
        result["child"] = convert_feature_to_result_document(statement.child)
    elif statement_type == "subscope":
        result["subscope"] = statement.scope

    return result


def convert_feature_to_result_document(feature):
    """
    "feature": {
        "number": 6,
        "type": "number"
    },

    "feature": {
        "api": "ws2_32.WSASocket",
        "type": "api"
    },

    "feature": {
        "match": "create TCP socket",
        "type": "match"
    },

    "feature": {
        "characteristic": [
            "loop",
            true
        ],
        "type": "characteristic"
    },
    """
    result = {"type": feature.name, feature.name: feature.get_value_str()}
    if feature.description:
        result["description"] = feature.description
    if feature.name == "regex":
        result["matches"] = feature.matches
    return result


def convert_node_to_result_document(node):
    """
    "node": {
        "type": "statement",
        "statement": { ... }
    },

    "node": {
        "type": "feature",
        "feature": { ... }
    },
    """

    if isinstance(node, capa.engine.Statement):
        return {
            "type": "statement",
            "statement": convert_statement_to_result_document(node),
        }
    elif isinstance(node, capa.features.Feature):
        return {
            "type": "feature",
            "feature": convert_feature_to_result_document(node),
        }
    else:
        raise RuntimeError("unexpected match node type")


def convert_match_to_result_document(rules, capabilities, result):
    """
    convert the given Result instance into a common, Python-native data structure.
    this will become part of the "result document" format that can be emitted to JSON.
    """
    doc = {
        "success": bool(result.success),
        "node": convert_node_to_result_document(result.statement),
        "children": [convert_match_to_result_document(rules, capabilities, child) for child in result.children],
    }

    # logic expression, like `and`, don't have locations - their children do.
    # so only add `locations` to feature nodes.
    if isinstance(result.statement, capa.features.Feature):
        if bool(result.success):
            doc["locations"] = result.locations
    elif isinstance(result.statement, capa.rules.Range):
        if bool(result.success):
            doc["locations"] = result.locations

    # if we have a `match` statement, then we're referencing another rule or namespace.
    # this could an external rule (written by a human), or
    #  rule generated to support a subscope (basic block, etc.)
    # we still want to include the matching logic in this tree.
    #
    # so, we need to lookup the other rule results
    # and then filter those down to the address used here.
    # finally, splice that logic into this tree.
    if (
        doc["node"]["type"] == "feature"
        and doc["node"]["feature"]["type"] == "match"
        # only add subtree on success,
        # because there won't be results for the other rule on failure.
        and doc["success"]
    ):

        name = doc["node"]["feature"]["match"]

        if name in rules:
            # this is a rule that we're matching
            #
            # pull matches from the referenced rule into our tree here.
            rule_name = doc["node"]["feature"]["match"]
            rule = rules[rule_name]
            rule_matches = {address: result for (address, result) in capabilities[rule_name]}

            if rule.meta.get("capa/subscope-rule"):
                # for a subscope rule, fixup the node to be a scope node, rather than a match feature node.
                #
                # e.g. `contain loop/30c4c78e29bf4d54894fc74f664c62e8` -> `basic block`
                scope = rule.meta["scope"]
                doc["node"] = {
                    "type": "statement",
                    "statement": {
                        "type": "subscope",
                        "subscope": scope,
                    },
                }

            for location in doc["locations"]:
                doc["children"].append(convert_match_to_result_document(rules, capabilities, rule_matches[location]))
        else:
            # this is a namespace that we're matching
            #
            # check for all rules in the namespace,
            # seeing if they matched.
            # if so, pull their matches into our match tree here.
            ns_name = doc["node"]["feature"]["match"]
            ns_rules = rules.rules_by_namespace[ns_name]

            for rule in ns_rules:
                if rule.name in capabilities:
                    # the rule matched, so splice results into our tree here.
                    #
                    # note, there's a shortcoming in our result document schema here:
                    # we lose the name of the rule that matched in a namespace.
                    # for example, if we have a statement: `match: runtime/dotnet`
                    # and we get matches, we can say the following:
                    #
                    #     match: runtime/dotnet @ 0x0
                    #       or:
                    #         import: mscoree._CorExeMain @ 0x402000
                    #
                    # however, we lose the fact that it was rule
                    #   "compiled to the .NET platform"
                    # that contained this logic and did the match.
                    #
                    # we could introduce an intermediate node here.
                    # this would be a breaking change and require updates to the renderers.
                    # in the meantime, the above might be sufficient.
                    rule_matches = {address: result for (address, result) in capabilities[rule.name]}
                    for location in doc["locations"]:
                        doc["children"].append(
                            convert_match_to_result_document(rules, capabilities, rule_matches[location])
                        )

    return doc


def convert_meta_to_result_document(meta):
    attacks = meta.get("att&ck", [])
    meta["att&ck"] = [parse_canonical_attack(attack) for attack in attacks]
    mbcs = meta.get("mbc", [])
    meta["mbc"] = [parse_canonical_mbc(mbc) for mbc in mbcs]
    return meta


def parse_canonical_attack(attack):
    """
    parse capa's canonical ATT&CK representation: `Tactic::Technique::Subtechnique [Identifier]`
    """
    tactic = ""
    technique = ""
    subtechnique = ""
    parts, id = capa.render.utils.parse_parts_id(attack)
    if len(parts) > 0:
        tactic = parts[0]
    if len(parts) > 1:
        technique = parts[1]
    if len(parts) > 2:
        subtechnique = parts[2]

    return {
        "parts": parts,
        "id": id,
        "tactic": tactic,
        "technique": technique,
        "subtechnique": subtechnique,
    }


def parse_canonical_mbc(mbc):
    """
    parse capa's canonical MBC representation: `Objective::Behavior::Method [Identifier]`
    """
    objective = ""
    behavior = ""
    method = ""
    parts, id = capa.render.utils.parse_parts_id(mbc)
    if len(parts) > 0:
        objective = parts[0]
    if len(parts) > 1:
        behavior = parts[1]
    if len(parts) > 2:
        method = parts[2]

    return {
        "parts": parts,
        "id": id,
        "objective": objective,
        "behavior": behavior,
        "method": method,
    }


def convert_capabilities_to_result_document(meta, rules, capabilities):
    """
    convert the given rule set and capabilities result to a common, Python-native data structure.
    this format can be directly emitted to JSON, or passed to the other `render_*` routines
     to render as text.

    see examples of substructures in above routines.

    schema:

    ```json
    {
      "meta": {...},
      "rules: {
        $rule-name: {
          "meta": {...copied from rule.meta...},
          "matches: {
            $address: {...match details...},
            ...
          }
        },
        ...
      }
    }
    ```

    Args:
      meta (Dict[str, Any]):
      rules (RuleSet):
      capabilities (Dict[str, List[Tuple[int, Result]]]):
    """
    doc = {
        "meta": meta,
        "rules": {},
    }

    for rule_name, matches in capabilities.items():
        rule = rules[rule_name]

        if rule.meta.get("capa/subscope-rule"):
            continue

        rule_meta = convert_meta_to_result_document(rule.meta)

        doc["rules"][rule_name] = {
            "meta": rule_meta,
            "source": rule.definition,
            "matches": {
                addr: convert_match_to_result_document(rules, capabilities, match) for (addr, match) in matches
            },
        }

    return doc


def render_vverbose(meta, rules, capabilities):
    # there's an import loop here
    # if capa.render imports capa.render.vverbose
    # and capa.render.vverbose import capa.render (implicitly, as a submodule)
    # so, defer the import until routine is called, breaking the import loop.
    import capa.render.vverbose

    doc = convert_capabilities_to_result_document(meta, rules, capabilities)
    return capa.render.vverbose.render_vverbose(doc)


def render_verbose(meta, rules, capabilities):
    # break import loop
    import capa.render.verbose

    doc = convert_capabilities_to_result_document(meta, rules, capabilities)
    return capa.render.verbose.render_verbose(doc)


def render_default(meta, rules, capabilities):
    # break import loop
    import capa.render.default
    import capa.render.verbose

    doc = convert_capabilities_to_result_document(meta, rules, capabilities)
    return capa.render.default.render_default(doc)


class CapaJsonObjectEncoder(json.JSONEncoder):
    """JSON encoder that emits Python sets as sorted lists"""

    def default(self, obj):
        if isinstance(obj, (list, dict, int, float, bool, type(None))) or isinstance(obj, str):
            return json.JSONEncoder.default(self, obj)
        elif isinstance(obj, set):
            return list(sorted(obj))
        else:
            # probably will TypeError
            return json.JSONEncoder.default(self, obj)


def render_json(meta, rules, capabilities):
    return json.dumps(
        convert_capabilities_to_result_document(meta, rules, capabilities),
        cls=CapaJsonObjectEncoder,
        sort_keys=True,
    )
