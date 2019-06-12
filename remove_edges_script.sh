#!/bin/bash

#SBATCH --job-name=removeEdges
#SBATCH --output=removeEdges%A_%a.out
#SBATCH --error=removeEdges%A_%a.err
#SBATCH --array=0-149
#SBATCH --time=01:00:00
#SBATCH --ntasks=1
#SBATCH --mem=16G
#SBATCH --mail-type ALL

datasets=({cora_ml,citeseer,ppi,pubmed,mit})
seeds=({0..29})

num_datasets=${#datasets[@]}
num_seeds=${#seeds[@]}

dataset_id=$((SLURM_ARRAY_TASK_ID / num_seeds % num_datasets ))
seed_id=$((SLURM_ARRAY_TASK_ID % (num_seed) ))

dataset=${datasets[$dataset_id]}
seed=${seeds[$seed_id]}

edgelist=datasets/${dataset}/edgelist.tsv
features=datasets/${dataset}/feats.csv
labels=datasets/${dataset}/labels.csv
output=edgelists/${dataset}/
python remove_edges.py --edgelist=$edgelist --features=$features --labels=$labels --output=$output --seed $seed
