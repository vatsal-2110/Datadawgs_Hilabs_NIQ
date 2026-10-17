"""C2 Step 0: profile one in-network rate file (facts only, no decisions).

Answers, in one streaming read of the file:
  * how many provider groups / NPIs / TINs / (NPI,TIN) pairs and billing codes it holds
  * is `network_name` ever filled? do business names carry network words ("PPO: Sacramento County")?
  * negotiation_arrangement (ffs / bundle / capitation), negotiated_type, billing_class, $0 rates, expiry dates
  * THE DECIDING TEST: does one provider get >1 different price for the same code under the same
    conditions (same negotiated_type, billing_class, modifiers, place of service)?  If yes, several
    contracts (possibly several networks) are mixed in the file; if no, it is one price list.

Outputs: data/stage2/profile_<name>.json and profile_<name>.txt

Usage (from project root):
  python -m src.stage2_profile_rate_file                         # default: Health Net CA in-network
  python -m src.stage2_profile_rate_file --file data/raw/rates/x.json --name x
"""
from __future__ import annotations

import argparse
import json
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

import ijson

from src.common.normalize import ROOT, load_config, norm_npi, norm_tin

RE_NET_WORD = re.compile(r"\b(PPO|HMO|EPO|POS|EOA|HSP|IPA|EXCLUSIVE|PREFERRED|NETWORK|SALUD|WHOLECARE|SMARTCARE)\b", re.I)
RE_LABEL_PREFIX = re.compile(r"^\s*([A-Za-z][A-Za-z ]{1,20}):\s")  # "PPO: Sacramento County" -> "PPO"


def pct(a, b):
    return round(100.0 * a / b, 2) if b else 0.0


def profile_provider_refs(path: Path):
    """Pass 1: provider_references. Returns (stats, ref_id -> tuple of (npi, tin) pairs)."""
    refs: dict[int, tuple] = {}
    ref_names: dict[int, str] = {}
    groups_per_ref, npis_per_group = Counter(), []
    tin_types, net_names = Counter(), Counter()
    word_hits, word_examples = Counter(), defaultdict(list)
    prefix_hits, prefix_examples = Counter(), defaultdict(list)
    npis, tins, bad_npi, bad_tin = set(), set(), 0, 0
    refs_with_netname = 0
    membership = Counter()
    with open(path, "rb") as f:
        for ref in ijson.items(f, "provider_references.item", use_float=True):
            rid = ref.get("provider_group_id")
            nn = [n for n in (ref.get("network_name") or []) if n]
            if nn:
                refs_with_netname += 1
                net_names.update(nn)
            pairs = []
            groups = ref.get("provider_groups") or []
            groups_per_ref[min(len(groups), 10)] += 1
            for g in groups:
                tin_obj = g.get("tin") or {}
                tin_types[tin_obj.get("type")] += 1
                tin = norm_tin(tin_obj.get("value"))
                if tin is None:
                    bad_tin += 1
                bn = tin_obj.get("business_name") or ""
                for w in {m.upper() for m in RE_NET_WORD.findall(bn)}:
                    word_hits[w] += 1
                    if len(word_examples[w]) < 8 and bn not in word_examples[w]:
                        word_examples[w].append(bn)
                m = RE_LABEL_PREFIX.match(bn)
                if m:
                    k = m.group(1).strip().upper()
                    prefix_hits[k] += 1
                    if len(prefix_examples[k]) < 8:
                        prefix_examples[k].append(bn)
                npi_list = g.get("npi") or []
                npis_per_group.append(len(npi_list))
                for raw in npi_list:
                    npi = norm_npi(raw)
                    if npi is None:
                        bad_npi += 1
                        continue
                    npis.add(npi)
                    pairs.append((npi, tin))
                if tin:
                    tins.add(tin)
            pairs_t = tuple(sorted(set(pairs)))
            refs[rid] = pairs_t
            ref_names[rid] = " / ".join(sorted({(g.get('tin') or {}).get('business_name') or '' for g in groups}))[:120]
            membership[pairs_t] += 1
    all_pairs = {p for t in refs.values() for p in t}
    npis_per_group.sort()
    n_grp = len(npis_per_group)
    dup_refs = sum(c - 1 for c in membership.values() if c > 1)
    stats = {
        "n_provider_references": len(refs),
        "provider_groups_per_reference_hist(10=10+)": dict(sorted(groups_per_ref.items())),
        "n_provider_groups": n_grp,
        "npis_per_group": {"min": npis_per_group[0] if n_grp else 0,
                           "median": npis_per_group[n_grp // 2] if n_grp else 0,
                           "p99": npis_per_group[int(n_grp * 0.99)] if n_grp else 0,
                           "max": npis_per_group[-1] if n_grp else 0},
        "n_unique_npi": len(npis), "n_unique_tin": len(tins), "n_unique_npi_tin_pairs": len(all_pairs),
        "invalid_npi_values": bad_npi, "invalid_tin_values": bad_tin,
        "tin_types": dict(tin_types),
        "references_with_network_name": refs_with_netname, "network_name_values": dict(net_names.most_common(30)),
        "references_with_identical_membership_to_another": dup_refs,
        "business_name_network_words": dict(word_hits.most_common()),
        "business_name_network_word_examples": dict(word_examples),
        "business_name_label_prefixes": dict(prefix_hits.most_common(30)),
        "business_name_label_prefix_examples": {k: prefix_examples[k] for k, _ in prefix_hits.most_common(30)},
    }
    return stats, refs, ref_names


def profile_in_network(path: Path, refs: dict, ref_names: dict, npi_sample_every: int):
    """Pass 2: in_network items (one per billing code)."""
    c = defaultdict(Counter)
    n_items = n_blocks = n_prices = n_zero = 0
    inline_groups_blocks = 0
    rates = []
    refs_used = set()
    blocks_per_item = Counter()
    refset_counter = Counter()
    # deciding test, provider-reference level (all items)
    ref_keys = ref_keys_multi = 0
    ref_in_multi_blocks = ref_item_total = 0
    # deciding test, (NPI,TIN) level (sampled items)
    pair_keys = pair_keys_multi = pair_keys_multiref = pair_keys_multi_from_refs = 0
    sampled_items = 0
    multi_examples = []
    with open(path, "rb") as f:
        for item in ijson.items(f, "in_network.item", use_float=True):
            n_items += 1
            code = item.get("billing_code")
            c["billing_code_type"][item.get("billing_code_type")] += 1
            c["negotiation_arrangement"][item.get("negotiation_arrangement")] += 1
            blocks = item.get("negotiated_rates") or []
            blocks_per_item[min(len(blocks), 50)] += 1
            n_blocks += len(blocks)
            ref_prices = defaultdict(lambda: defaultdict(set))  # ref -> key -> {rates}
            ref_block_count = Counter()
            for b in blocks:
                rlist = b.get("provider_references") or []
                if b.get("provider_groups"):
                    inline_groups_blocks += 1
                refs_used.update(rlist)
                refset_counter[hash(tuple(sorted(rlist)))] += 1
                for r in set(rlist):
                    ref_block_count[r] += 1
                for p in b.get("negotiated_prices") or []:
                    n_prices += 1
                    rate = p.get("negotiated_rate")
                    nt, bc = p.get("negotiated_type"), p.get("billing_class")
                    c["negotiated_type"][nt] += 1
                    c["billing_class"][bc] += 1
                    c["expiration_date"][p.get("expiration_date")] += 1
                    c["has_service_code"][bool(p.get("service_code"))] += 1
                    c["has_modifier"][bool(p.get("billing_code_modifier"))] += 1
                    c["setting"][p.get("setting")] += 1  # not in v2 schema; counted in case present
                    if rate is not None:
                        rates.append(float(rate))
                        if float(rate) == 0.0:
                            n_zero += 1
                    # "same conditions" = type, class, modifiers, place of service, AND additional_information
                    # (Health Net puts sub-codes there, e.g. home-health HIPPS "1EA11": a different service, not a
                    # different contract; leaving it out made sub-prices look like multiple networks)
                    c["has_additional_information"][bool(p.get("additional_information"))] += 1
                    key = (nt, bc, tuple(sorted(p.get("billing_code_modifier") or [])),
                           tuple(sorted(p.get("service_code") or [])), p.get("additional_information"))
                    for r in rlist:
                        ref_prices[r][key].add(rate)
            for r, kd in ref_prices.items():
                for key, s in kd.items():
                    ref_keys += 1
                    if len(s) > 1:
                        ref_keys_multi += 1
            ref_item_total += len(ref_block_count)
            ref_in_multi_blocks += sum(1 for v in ref_block_count.values() if v > 1)
            if n_items % npi_sample_every == 0:  # (NPI,TIN) level, sampled
                sampled_items += 1
                pair_prices = defaultdict(set)
                pair_refs = defaultdict(set)
                for r, kd in ref_prices.items():
                    for key, s in kd.items():
                        for pair in refs.get(r, ()):
                            pair_prices[(pair, key)].update(s)
                            pair_refs[(pair, key)].add(r)
                pair_keys += len(pair_prices)
                for pk, s in pair_prices.items():
                    if len(pair_refs[pk]) > 1:
                        pair_keys_multiref += 1
                    if len(s) > 1:
                        pair_keys_multi += 1
                        if len(pair_refs[pk]) > 1:
                            pair_keys_multi_from_refs += 1
                        if len(multi_examples) < 12:
                            (pair, key) = pk
                            multi_examples.append({"billing_code": code, "item_name": item.get("name"),
                                                   "npi": pair[0], "tin": pair[1], "provider_refs": sorted(pair_refs[pk]),
                                                   "business_names": sorted({ref_names.get(r, "") for r in pair_refs[pk]}),
                                                   "billing_class": key[1], "modifiers": list(key[2]),
                                                   "service_codes": list(key[3])[:5], "additional_information": key[4],
                                                   "rates": sorted(x for x in s if x is not None)[:10]})
    rates.sort()
    n = len(rates)
    q = (lambda x: rates[min(int(n * x), n - 1)]) if n else (lambda x: None)
    exp = c.pop("expiration_date")
    stats = {
        "n_in_network_items(billing codes)": n_items,
        "n_negotiated_rate_blocks": n_blocks, "n_negotiated_prices": n_prices,
        "blocks_per_item_hist(50=50+)": dict(sorted(blocks_per_item.items())),
        "blocks_with_inline_provider_groups": inline_groups_blocks,
        "n_provider_references_used": len(refs_used),
        "n_provider_references_defined_but_unused": len(set(refs) - refs_used),
        "n_distinct_provider_reference_sets(contract-group fingerprints)": len(refset_counter),
        **{k: dict(v.most_common(25)) for k, v in c.items()},
        "expiration_date_top": dict(exp.most_common(10)), "n_distinct_expiration_dates": len(exp),
        "zero_rates": n_zero, "zero_rate_pct": pct(n_zero, n),
        "rate_quantiles": {"p01": q(.01), "p25": q(.25), "median": q(.5), "p75": q(.75), "p99": q(.99),
                           "max": rates[-1] if n else None},
        "DECIDING_TEST": {
            "ref_level(all items): (item, provider_ref, conditions) combos": ref_keys,
            "ref_level: combos with >1 distinct rate": ref_keys_multi,
            "ref_level: pct_multi": pct(ref_keys_multi, ref_keys),
            "ref_level: (item, ref) pairs where ref appears in >1 block": ref_in_multi_blocks,
            "ref_level: pct_ref_in_multi_blocks": pct(ref_in_multi_blocks, ref_item_total),
            f"pair_level(1 in {npi_sample_every} items, {sampled_items} items): (item, npi, tin, conditions) combos": pair_keys,
            "pair_level: combos with >1 distinct rate": pair_keys_multi,
            "pair_level: pct_multi": pct(pair_keys_multi, pair_keys),
            "pair_level: combos where the pair sits in >1 provider_ref": pair_keys_multiref,
            "pair_level: combos with >1 rate coming from different provider_refs": pair_keys_multi_from_refs,
            "pair_level: pct_multi_from_different_refs": pct(pair_keys_multi_from_refs, pair_keys),
            "pair_level: examples": multi_examples,
        },
    }
    return stats


HEADER_KEYS = ("reporting_entity_name", "reporting_entity_type", "plan_name", "plan_id", "plan_market_type",
               "last_updated_on", "version")


def read_header(path: Path) -> dict:
    """Top-level scalars from the first/last 8 KB, and byte offsets of the two big arrays (fast)."""
    size = path.stat().st_size
    with open(path, "rb") as f:
        head = f.read(8192).decode("utf-8", "ignore")
        f.seek(max(0, size - 8192))
        tail = f.read().decode("utf-8", "ignore")
    out = {}
    for k in HEADER_KEYS:
        m = re.search(rf'"{k}"\s*:\s*"([^"]*)"', head) or re.search(rf'"{k}"\s*:\s*"([^"]*)"', tail)
        if m:
            out[k] = m.group(1)
    offsets = {}
    with open(path, "rb") as f:  # first occurrence of each array key
        pos, prev = 0, b""
        while len(offsets) < 2:
            chunk = f.read(1 << 24)
            if not chunk:
                break
            buf = prev + chunk
            for k in (b'"provider_references"', b'"in_network"'):
                if k not in offsets and (i := buf.find(k)) >= 0:
                    offsets[k] = pos - len(prev) + i
            prev, pos = buf[-32:], pos + len(chunk)
    out["array_offsets"] = {k.decode().strip('"'): v for k, v in offsets.items()}
    return out


def to_text(name: str, prof: dict) -> str:
    L = [f"Rate-file profile: {name}", "=" * 78]

    def dump(d, ind=0):
        for k, v in d.items():
            if isinstance(v, dict) and v and all(not isinstance(x, (dict, list)) for x in v.values()) and len(v) <= 12:
                L.append(" " * ind + f"{k}: " + ", ".join(f"{a}={b}" for a, b in v.items()))
            elif isinstance(v, dict):
                L.append(" " * ind + f"{k}:")
                dump(v, ind + 2)
            elif isinstance(v, list):
                L.append(" " * ind + f"{k}:")
                for x in v:
                    L.append(" " * (ind + 2) + "- " + (json.dumps(x) if isinstance(x, dict) else str(x)))
            else:
                L.append(" " * ind + f"{k}: {v}")
    dump(prof)
    return "\n".join(L) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", default="data/raw/rates/2026-09-30_centene-management-company-llc_healthnet-ca_in-network.json")
    ap.add_argument("--name", default="healthnet-ca_in-network")
    ap.add_argument("--npi-sample-every", type=int, default=1,
                    help="run the (NPI,TIN)-level deciding test on 1 in N billing codes (default 1 = all)")
    args = ap.parse_args(argv)
    cfg = load_config()
    path = ROOT / args.file
    out = cfg["paths"]["stage2"]
    out.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    prof = {"file": Path(args.file).as_posix(), "bytes": path.stat().st_size}
    prof["header"] = read_header(path)
    print(f"header done {time.time()-t0:.0f}s")
    ref_stats, refs, ref_names = profile_provider_refs(path)
    prof["provider_references"] = ref_stats
    print(f"pass 1 (provider_references) done {time.time()-t0:.0f}s")
    prof["in_network"] = profile_in_network(path, refs, ref_names, args.npi_sample_every)
    prof["runtime_s"] = round(time.time() - t0)
    print(f"pass 2 (in_network) done {time.time()-t0:.0f}s")

    (out / f"profile_{args.name}.json").write_text(json.dumps(prof, indent=2, default=str), encoding="utf-8")
    text = to_text(args.name, prof)
    (out / f"profile_{args.name}.txt").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
